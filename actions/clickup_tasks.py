"""
actions/clickup_tasks.py
------------------------
Creates ClickUp tasks for downstream follow-up actions:
  - Dropbox save failures  -> NaTarsha
  - Training triggers       -> Supervisors
  - Insurance changes       -> Life coaches
  - Patient-consolidated tasks (pre-billing, claim issues)

Patient task consolidation:
  - Tracks ClickUp tasks per patient in SQLite (clickup_patient_tasks table)
  - Before creating a new task, checks if one already exists for the same patient
  - If exists: adds a comment to the existing task instead of creating new one
  - Includes: what's been done autonomously (with dates), what's needed, claim history
"""
from __future__ import annotations

import os
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

import aiohttp

from config.settings import (
    CLICKUP_API_TOKEN,
    CLICKUP_WORKSPACE_ID,
    DRY_RUN,
)
from logging_utils.logger import get_logger

# Configurable list ID — set CLICKUP_LIST_ID in .env to target a specific list
CLICKUP_LIST_ID = os.getenv("CLICKUP_LIST_ID", "")

# SQLite path for patient task tracking
_DB_DIR = Path(__file__).resolve().parent.parent / "data"
_DB_DIR.mkdir(parents=True, exist_ok=True)
_DB_PATH = _DB_DIR / "claims_history.db"

# ClickUp priority values: 1 = Urgent, 2 = High, 3 = Normal, 4 = Low
PRIORITY_URGENT = 1
PRIORITY_HIGH = 2
PRIORITY_NORMAL = 3
PRIORITY_LOW = 4

# ClickUp workspace member IDs
MEMBER_NICHOLAS = 48215738
MEMBER_DESIREE = 30050728
MEMBER_JUSTIN = 48206027
MEMBER_NATARSHA_W = 105978072  # NaTarsha Williams
MEMBER_NATARSHA_M = 198206669  # Nartarshia McCrey

# Default assignees when no specific person is known
DEFAULT_ASSIGNEES = [MEMBER_NICHOLAS, MEMBER_DESIREE, MEMBER_JUSTIN]

# Role-based assignee mapping
ASSIGNEE_MAP = {
    "nicholas": [MEMBER_NICHOLAS],
    "desiree": [MEMBER_DESIREE],
    "justin": [MEMBER_JUSTIN],
    "natarsha": [MEMBER_NATARSHA_W],
    "natarsha_w": [MEMBER_NATARSHA_W],
    "natarsha_m": [MEMBER_NATARSHA_M],
    "billing": [MEMBER_DESIREE],
    "bank_verify": [MEMBER_JUSTIN],
    "intake": [MEMBER_NATARSHA_W],
    "dropbox": [MEMBER_NATARSHA_W],
    "entity_fix": [MEMBER_JUSTIN],
    "write_off_approval": [MEMBER_DESIREE],
    "training": [MEMBER_DESIREE, MEMBER_NICHOLAS],
    "insurance_change": [MEMBER_NICHOLAS, MEMBER_DESIREE],
}

logger = get_logger("clickup_tasks")


def get_assignees(role: str = "") -> List[int]:
    """Get ClickUp member IDs for a given role. Defaults to Nicholas + Desiree + Justin."""
    if role and role.lower() in ASSIGNEE_MAP:
        return ASSIGNEE_MAP[role.lower()]
    return DEFAULT_ASSIGNEES


def _next_business_day(from_date: Optional[date] = None) -> datetime:
    """Return the next business day (Mon-Fri) as a datetime at 17:00 ET."""
    d = from_date or date.today()
    d += timedelta(days=1)
    while d.weekday() >= 5:  # 5 = Saturday, 6 = Sunday
        d += timedelta(days=1)
    return datetime(d.year, d.month, d.day, 17, 0, 0)


def _to_clickup_timestamp(dt: datetime) -> int:
    """Convert datetime to ClickUp-style Unix ms timestamp."""
    return int(dt.timestamp() * 1000)


def _ensure_patient_tasks_table():
    """Create the clickup_patient_tasks table if it doesn't exist."""
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS clickup_patient_tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            patient_name TEXT NOT NULL,
            task_id TEXT NOT NULL,
            task_url TEXT DEFAULT '',
            status TEXT DEFAULT 'open',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
    """)
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_patient_task_name "
        "ON clickup_patient_tasks(patient_name)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_patient_task_status "
        "ON clickup_patient_tasks(status)"
    )
    conn.commit()
    conn.close()


_ensure_patient_tasks_table()


class ClickUpTaskCreator:
    """Creates ClickUp tasks via the v2 API."""

    BASE_URL = "https://api.clickup.com/api/v2"

    def __init__(self, list_id: str = CLICKUP_LIST_ID):
        self.list_id = list_id
        self.token = CLICKUP_API_TOKEN
        self.workspace_id = CLICKUP_WORKSPACE_ID
        self.log = get_logger("clickup_task_creator")

    # ------------------------------------------------------------------
    # Core task creation
    # ------------------------------------------------------------------

    async def create_task(
        self,
        list_id: str,
        name: str,
        description: str,
        assignees: Optional[List[int]] = None,
        due_date: Optional[datetime] = None,
        priority: Optional[int] = None,
    ) -> Optional[str]:
        """
        Create a task in the given ClickUp list.

        Returns the new task ID on success, or None on failure.
        """
        if not self.token:
            self.log.error("No CLICKUP_API_TOKEN configured — cannot create task")
            return None

        if not list_id:
            self.log.error("No list_id provided — cannot create task")
            return None

        payload: dict = {
            "name": name,
            "description": description,
            "notify_all": False,
        }
        if assignees:
            payload["assignees"] = assignees
        if due_date is not None:
            payload["due_date"] = _to_clickup_timestamp(due_date)
            payload["due_date_time"] = True
        if priority is not None:
            payload["priority"] = priority

        if DRY_RUN:
            self.log.info(
                "DRY_RUN: Would create ClickUp task",
                list_id=list_id,
                name=name,
                priority=priority,
                due_date=str(due_date),
            )
            return "dry-run-task-id"

        url = f"{self.BASE_URL}/list/{list_id}/task"
        headers = {
            "Authorization": self.token,
            "Content-Type": "application/json",
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status in (200, 201):
                    data = await resp.json()
                    task_id = data.get("id")
                    self.log.info(
                        "ClickUp task created",
                        task_id=task_id,
                        name=name,
                        list_id=list_id,
                    )
                    return task_id
                else:
                    body = await resp.text()
                    self.log.error(
                        "ClickUp task creation failed",
                        status=resp.status,
                        body=body[:300],
                        name=name,
                        list_id=list_id,
                    )
                    return None

    # ------------------------------------------------------------------
    # Dropbox save failures -> NaTarsha
    # ------------------------------------------------------------------

    async def create_natarsha_dropbox_task(
        self, clients_missing: List[str]
    ) -> Optional[str]:
        """
        Create a task for NaTarsha when auth documents were submitted
        via portal but not saved to Dropbox.

        Args:
            clients_missing: list of client names/IDs whose docs are missing.
        """
        count = len(clients_missing)
        today = date.today().strftime("%m/%d/%y")
        due = _next_business_day()

        name = f"Dropbox Save Failure — {count} client(s) [{today}]"

        client_bullets = "\n".join(f"  - {c}" for c in clients_missing)
        description = (
            f"Auth was submitted via portal but NOT saved to Dropbox for "
            f"{count} client(s).\n\n"
            f"Affected clients:\n{client_bullets}\n\n"
            f"Action needed: Verify auth was received, re-download from portal, "
            f"and save to the correct Dropbox folder.\n\n"
            f"Generated by Claims Automation on {today}."
        )

        self.log.info(
            "Creating Dropbox save failure task",
            clients_missing=count,
            due_date=str(due),
        )

        return await self.create_task(
            list_id=self.list_id,
            name=name,
            description=description,
            assignees=get_assignees("dropbox"),
            due_date=due,
            priority=PRIORITY_HIGH,
        )

    # ------------------------------------------------------------------
    # Training triggers -> Supervisors
    # ------------------------------------------------------------------

    async def create_training_flag_task(
        self,
        staff_name: str,
        gap_category: str,
        count: int,
        claim_ids: List[str],
    ) -> Optional[str]:
        """
        Create a task for supervisors when a staff member hits 3+ of the
        same gap category within 30 days (training trigger).

        Args:
            staff_name:   Name of the staff member.
            gap_category: The repeated gap category (e.g. "Missing Auth").
            count:        Number of occurrences in the 30-day window.
            claim_ids:    Related claim IDs.
        """
        today = date.today().strftime("%m/%d/%y")
        due = _next_business_day()

        name = f"Training Flag — {staff_name}: {gap_category} ({count}x) [{today}]"

        claims_bullets = "\n".join(f"  - {cid}" for cid in claim_ids)
        description = (
            f"Staff member {staff_name} has hit {count} occurrences of "
            f"\"{gap_category}\" within the last 30 days, triggering a "
            f"training review.\n\n"
            f"Related claims:\n{claims_bullets}\n\n"
            f"Action needed: Review the pattern with the staff member and "
            f"provide targeted coaching/training.\n\n"
            f"Generated by Claims Automation on {today}."
        )

        self.log.info(
            "Creating training flag task",
            staff_name=staff_name,
            gap_category=gap_category,
            count=count,
        )

        return await self.create_task(
            list_id=self.list_id,
            name=name,
            description=description,
            assignees=get_assignees("training"),
            due_date=due,
            priority=PRIORITY_NORMAL,
        )

    # ------------------------------------------------------------------
    # Insurance changes -> Life coaches
    # ------------------------------------------------------------------

    async def create_insurance_change_task(
        self,
        client_name: str,
        old_mco: str,
        new_mco: str,
        life_coach: str,
        client_id: str = "",
    ) -> Optional[str]:
        """
        Create a task for a life coach when a client's insurance coverage
        has terminated or changed MCOs.

        Args:
            client_name: Client's name.
            old_mco:     Previous MCO / insurance.
            new_mco:     New MCO, or "Terminated" / "None" if coverage ended.
            life_coach:  Name of the assigned life coach.
            client_id:   Client's Medicaid / record number.
        """
        today = date.today().strftime("%m/%d/%y")
        due = _next_business_day()

        name = f"Insurance Change — {client_name}: {old_mco} -> {new_mco} [{today}]"

        client_id_line = f"Lauris Unique ID: {client_id}\n" if client_id else ""
        description = (
            f"Insurance change detected for {client_name}.\n"
            f"{client_id_line}\n"
            f"Previous MCO: {old_mco}\n"
            f"New MCO: {new_mco}\n"
            f"Assigned Life Coach: {life_coach}\n\n"
            f"Action needed: Contact {client_name} to verify coverage status "
            f"and update service authorizations as needed. If coverage is "
            f"terminated, coordinate with the team on next steps.\n\n"
            f"Generated by Claims Automation on {today}."
        )

        self.log.info(
            "Creating insurance change task",
            client_name=client_name,
            old_mco=old_mco,
            new_mco=new_mco,
            life_coach=life_coach,
        )

        return await self.create_task(
            list_id=self.list_id,
            name=name,
            description=description,
            assignees=get_assignees("insurance_change"),
            due_date=due,
            priority=PRIORITY_HIGH,
        )

    # ------------------------------------------------------------------
    # Patient task consolidation
    # ------------------------------------------------------------------

    def get_existing_task_for_patient(self, patient_name: str) -> Optional[str]:
        """
        Check SQLite for an existing open ClickUp task for this patient.

        Returns the task_id if one exists and is still open, or None.
        """
        conn = sqlite3.connect(str(_DB_PATH))
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            """SELECT task_id FROM clickup_patient_tasks
               WHERE patient_name = ? AND status = 'open'
               ORDER BY updated_at DESC LIMIT 1""",
            (patient_name,),
        ).fetchone()
        conn.close()

        if row:
            self.log.info(
                "Found existing ClickUp task for patient",
                patient_name=patient_name,
                task_id=row["task_id"],
            )
            return row["task_id"]
        return None

    async def add_comment_to_task(self, task_id: str, comment: str) -> bool:
        """
        Add a comment to an existing ClickUp task.

        Returns True on success, False on failure.
        """
        if not self.token:
            self.log.error("No CLICKUP_API_TOKEN — cannot add comment")
            return False

        if DRY_RUN:
            self.log.info(
                "DRY_RUN: Would add comment to ClickUp task",
                task_id=task_id,
                comment_preview=comment[:100],
            )
            return True

        url = f"{self.BASE_URL}/task/{task_id}/comment"
        headers = {
            "Authorization": self.token,
            "Content-Type": "application/json",
        }
        payload = {
            "comment_text": comment,
            "notify_all": False,
        }

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status in (200, 201):
                    self.log.info(
                        "Comment added to ClickUp task",
                        task_id=task_id,
                    )
                    # Update the timestamp in SQLite
                    conn = sqlite3.connect(str(_DB_PATH))
                    conn.execute(
                        "UPDATE clickup_patient_tasks SET updated_at = ? WHERE task_id = ?",
                        (datetime.now().isoformat(), task_id),
                    )
                    conn.commit()
                    conn.close()
                    return True
                else:
                    body = await resp.text()
                    self.log.error(
                        "Failed to add comment to ClickUp task",
                        task_id=task_id,
                        status=resp.status,
                        body=body[:300],
                    )
                    return False

    async def create_or_update_patient_task(
        self,
        patient_name: str,
        claim_id: str,
        issue: str,
        history: str,
        role: str = "",
        client_id: str = "",
    ) -> str:
        """
        Create a new ClickUp task for the patient, or add a comment to
        the existing one if a task is already open.

        Always includes:
          - What's been done autonomously (with dates)
          - What's needed
          - Claim history

        Default due date: 1 business day.

        Returns the task_id (new or existing).
        """
        today = date.today().strftime("%m/%d/%y")
        due = _next_business_day()

        existing_task_id = self.get_existing_task_for_patient(patient_name)

        if existing_task_id:
            # Add comment to existing task
            client_id_line = f"Lauris Unique ID: {client_id}\n" if client_id else ""
            comment = (
                f"UPDATE ({today}) — Claim {claim_id}\n"
                f"{client_id_line}\n"
                f"Issue:\n{issue}\n\n"
                f"Autonomous actions taken:\n{history}\n\n"
                f"Generated by Claims Automation on {today}."
            )
            success = await self.add_comment_to_task(existing_task_id, comment)
            if success:
                self.log.info(
                    "Updated existing patient task",
                    patient_name=patient_name,
                    task_id=existing_task_id,
                    claim_id=claim_id,
                )
                return existing_task_id
            # If comment failed, fall through and create a new task
            self.log.warning(
                "Failed to update existing task — creating new one",
                patient_name=patient_name,
                task_id=existing_task_id,
            )

        # Create new task
        name = f"Claims Issue — {patient_name} [{today}]"
        client_id_line = f"Lauris Unique ID: {client_id}\n" if client_id else ""
        description = (
            f"Patient: {patient_name}\n"
            f"{client_id_line}"
            f"Claim(s): {claim_id}\n\n"
            f"Issue:\n{issue}\n\n"
            f"Autonomous actions taken:\n{history}\n\n"
            f"What's needed: Manual review and resolution of the above issue(s).\n\n"
            f"Generated by Claims Automation on {today}."
        )

        self.log.info(
            "Creating new patient task",
            patient_name=patient_name,
            claim_id=claim_id,
        )

        task_id = await self.create_task(
            list_id=self.list_id,
            name=name,
            description=description,
            assignees=get_assignees(role),
            due_date=due,
            priority=PRIORITY_HIGH,
        )

        if task_id:
            # Record in SQLite
            now = datetime.now().isoformat()
            conn = sqlite3.connect(str(_DB_PATH))
            conn.execute(
                """INSERT INTO clickup_patient_tasks
                   (patient_name, task_id, status, created_at, updated_at)
                   VALUES (?, ?, 'open', ?, ?)""",
                (patient_name, task_id, now, now),
            )
            conn.commit()
            conn.close()
            self.log.info(
                "Patient task recorded in SQLite",
                patient_name=patient_name,
                task_id=task_id,
            )

        return task_id or ""
