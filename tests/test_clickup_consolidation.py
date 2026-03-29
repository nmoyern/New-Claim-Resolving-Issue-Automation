"""
tests/test_clickup_consolidation.py
------------------------------------
Tests for ClickUp patient task consolidation.
"""
import os
import sqlite3
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch, MagicMock

import pytest

os.environ.setdefault("DRY_RUN", "true")

from actions.clickup_tasks import (
    ClickUpTaskCreator,
    _next_business_day,
    _ensure_patient_tasks_table,
    _DB_PATH,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def tmp_db(tmp_path):
    """Create a temporary SQLite DB for patient task tracking."""
    db_path = tmp_path / "test_clickup.db"
    conn = sqlite3.connect(str(db_path))
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
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def creator():
    """Create a ClickUpTaskCreator in DRY_RUN mode."""
    return ClickUpTaskCreator(list_id="test-list-123")


# ---------------------------------------------------------------------------
# get_existing_task_for_patient tests
# ---------------------------------------------------------------------------

class TestGetExistingTask:

    def test_returns_none_when_no_task(self, creator, tmp_db):
        with patch("actions.clickup_tasks._DB_PATH", tmp_db):
            result = creator.get_existing_task_for_patient("Unknown Patient")
        assert result is None

    def test_returns_task_id_when_exists(self, creator, tmp_db):
        # Insert a task
        conn = sqlite3.connect(str(tmp_db))
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO clickup_patient_tasks
               (patient_name, task_id, status, created_at, updated_at)
               VALUES (?, ?, 'open', ?, ?)""",
            ("John Doe", "task-abc-123", now, now),
        )
        conn.commit()
        conn.close()

        with patch("actions.clickup_tasks._DB_PATH", tmp_db):
            result = creator.get_existing_task_for_patient("John Doe")
        assert result == "task-abc-123"

    def test_ignores_closed_tasks(self, creator, tmp_db):
        conn = sqlite3.connect(str(tmp_db))
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO clickup_patient_tasks
               (patient_name, task_id, status, created_at, updated_at)
               VALUES (?, ?, 'closed', ?, ?)""",
            ("Jane Doe", "task-closed-1", now, now),
        )
        conn.commit()
        conn.close()

        with patch("actions.clickup_tasks._DB_PATH", tmp_db):
            result = creator.get_existing_task_for_patient("Jane Doe")
        assert result is None


# ---------------------------------------------------------------------------
# add_comment_to_task tests (DRY_RUN)
# ---------------------------------------------------------------------------

class TestAddComment:

    @pytest.mark.asyncio
    async def test_dry_run_returns_true(self, creator):
        result = await creator.add_comment_to_task("task-123", "Test comment")
        assert result is True

    @pytest.mark.asyncio
    async def test_no_token_returns_false(self):
        creator = ClickUpTaskCreator(list_id="test")
        creator.token = ""
        result = await creator.add_comment_to_task("task-123", "Test")
        assert result is False


# ---------------------------------------------------------------------------
# create_or_update_patient_task tests (DRY_RUN)
# ---------------------------------------------------------------------------

class TestCreateOrUpdatePatientTask:

    @pytest.mark.asyncio
    async def test_creates_new_task_when_none_exists(self, creator, tmp_db):
        with patch("actions.clickup_tasks._DB_PATH", tmp_db):
            task_id = await creator.create_or_update_patient_task(
                patient_name="New Patient",
                claim_id="CLM001",
                issue="Missing auth",
                history="Checked portal on 03/22/26",
            )
        assert task_id == "dry-run-task-id"

    @pytest.mark.asyncio
    async def test_updates_existing_task(self, creator, tmp_db):
        # Insert existing task
        conn = sqlite3.connect(str(tmp_db))
        now = datetime.now().isoformat()
        conn.execute(
            """INSERT INTO clickup_patient_tasks
               (patient_name, task_id, status, created_at, updated_at)
               VALUES (?, ?, 'open', ?, ?)""",
            ("Existing Patient", "task-existing-1", now, now),
        )
        conn.commit()
        conn.close()

        with patch("actions.clickup_tasks._DB_PATH", tmp_db):
            task_id = await creator.create_or_update_patient_task(
                patient_name="Existing Patient",
                claim_id="CLM002",
                issue="Wrong entity",
                history="Checked Lauris on 03/22/26",
            )
        assert task_id == "task-existing-1"


# ---------------------------------------------------------------------------
# _next_business_day tests
# ---------------------------------------------------------------------------

class TestNextBusinessDay:

    def test_returns_datetime(self):
        result = _next_business_day()
        assert isinstance(result, datetime)

    def test_not_weekend(self):
        result = _next_business_day()
        assert result.weekday() < 5  # Mon-Fri


# ---------------------------------------------------------------------------
# Table creation test
# ---------------------------------------------------------------------------

class TestTableCreation:

    def test_patient_tasks_table_exists(self):
        """Verify the clickup_patient_tasks table is created at import time."""
        conn = sqlite3.connect(str(_DB_PATH))
        cursor = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='clickup_patient_tasks'"
        )
        result = cursor.fetchone()
        conn.close()
        assert result is not None
