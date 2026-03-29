"""
tests/test_self_learning.py
----------------------------
Tests for the self-learning and efficiency module.
"""
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

os.environ.setdefault("DRY_RUN", "true")

from reporting.self_learning import (
    increment_run_count,
    should_generate_report,
    analyze_decision_outcomes,
    identify_patterns,
    estimate_financial_impact,
    generate_self_learning_report,
    email_report,
    RUN_COUNTER_PATH,
)


# ---------------------------------------------------------------------------
# Run counter tests
# ---------------------------------------------------------------------------

class TestRunCounter:

    def setup_method(self):
        """Use a temp file for run counter so tests don't interfere."""
        self._tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".txt")
        self._tmp.close()
        self._patch = patch(
            "reporting.self_learning.RUN_COUNTER_PATH",
            Path(self._tmp.name),
        )
        self._mock_path = self._patch.start()

    def teardown_method(self):
        self._patch.stop()
        try:
            os.unlink(self._tmp.name)
        except OSError:
            pass

    def test_increment_from_zero(self):
        Path(self._tmp.name).write_text("0")
        result = increment_run_count()
        assert result == 1

    def test_increment_sequential(self):
        Path(self._tmp.name).write_text("5")
        result = increment_run_count()
        assert result == 6

    def test_increment_creates_file(self):
        # Start with empty/missing content
        Path(self._tmp.name).write_text("")
        result = increment_run_count()
        assert result == 1

    def test_should_generate_report_at_10(self):
        Path(self._tmp.name).write_text("10")
        assert should_generate_report() is True

    def test_should_generate_report_at_20(self):
        Path(self._tmp.name).write_text("20")
        assert should_generate_report() is True

    def test_should_not_generate_report_at_5(self):
        Path(self._tmp.name).write_text("5")
        assert should_generate_report() is False

    def test_should_not_generate_report_at_0(self):
        Path(self._tmp.name).write_text("0")
        assert should_generate_report() is False

    def test_should_generate_report_at_100(self):
        Path(self._tmp.name).write_text("100")
        assert should_generate_report() is True


# ---------------------------------------------------------------------------
# Analysis tests (with in-memory DB)
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_db(tmp_path):
    """Create an in-memory-like temp DB with test data."""
    db_path = tmp_path / "test_claims.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE claim_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            claim_id TEXT NOT NULL,
            date TEXT NOT NULL,
            action_taken TEXT NOT NULL,
            result TEXT NOT NULL,
            note_written TEXT DEFAULT '',
            gap_category TEXT DEFAULT '',
            dollar_amount REAL DEFAULT 0.0,
            created_at TEXT NOT NULL,
            denial_raw TEXT DEFAULT ''
        );
        CREATE TABLE gap_report (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            date TEXT NOT NULL,
            claim_id TEXT NOT NULL,
            client_name TEXT NOT NULL,
            mco TEXT NOT NULL,
            program TEXT NOT NULL,
            denial_type TEXT NOT NULL,
            gap_category TEXT NOT NULL,
            staff_responsible TEXT DEFAULT '',
            dollar_amount REAL DEFAULT 0.0,
            resolution TEXT DEFAULT '',
            lauris_fix TEXT DEFAULT '',
            status TEXT DEFAULT 'pending',
            recurrence_flag INTEGER DEFAULT 0,
            created_at TEXT NOT NULL
        );
    """)

    # Insert test data spanning 6 months
    today = date.today()
    for i in range(50):
        d = (today - timedelta(days=i * 5)).isoformat()
        action = "correct_and_resubmit" if i % 3 == 0 else "reconsideration"
        result = "success" if i % 2 == 0 else "failed"
        conn.execute(
            """INSERT INTO claim_history
               (claim_id, date, action_taken, result, dollar_amount, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (f"CLM{i:03d}", d, action, result, 100.0 + i * 10, datetime.now().isoformat()),
        )

    for i in range(30):
        d = (today - timedelta(days=i * 7)).isoformat()
        client = "John Doe" if i % 5 == 0 else f"Client {i}"
        gap_cat = "AUTH -- Never Submitted" if i % 3 == 0 else "BILLING -- Wrong Member ID"
        status = "resolved" if i % 2 == 0 else "write_off"
        conn.execute(
            """INSERT INTO gap_report
               (date, claim_id, client_name, mco, program, denial_type,
                gap_category, dollar_amount, resolution, status, recurrence_flag, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (d, f"CLM{i:03d}", client, "sentara", "NHCS", "no_auth",
             gap_cat, 200.0 + i * 5, "reconsideration", status, 0,
             datetime.now().isoformat()),
        )

    conn.commit()
    conn.close()
    return db_path


class TestAnalyzeDecisionOutcomes:

    def test_returns_dict_structure(self, mock_db):
        with patch("reporting.self_learning.DB_PATH", mock_db):
            result = analyze_decision_outcomes()
        assert "action_outcomes" in result
        assert "top_resolving_actions" in result
        assert "top_failing_actions" in result
        assert "denial_to_action_map" in result

    def test_action_outcomes_has_data(self, mock_db):
        with patch("reporting.self_learning.DB_PATH", mock_db):
            result = analyze_decision_outcomes()
        outcomes = result["action_outcomes"]
        assert len(outcomes) > 0
        for action, data in outcomes.items():
            assert "total" in data
            assert "success" in data
            assert "failed" in data
            assert "resolution_rate" in data
            assert data["total"] == data["success"] + data["failed"]

    def test_resolution_rate_is_percentage(self, mock_db):
        with patch("reporting.self_learning.DB_PATH", mock_db):
            result = analyze_decision_outcomes()
        for action, data in result["action_outcomes"].items():
            assert 0 <= data["resolution_rate"] <= 100


class TestIdentifyPatterns:

    def test_returns_list(self, mock_db):
        with patch("reporting.self_learning.DB_PATH", mock_db):
            result = identify_patterns()
        assert isinstance(result, list)

    def test_pattern_structure(self, mock_db):
        with patch("reporting.self_learning.DB_PATH", mock_db):
            result = identify_patterns()
        for p in result:
            assert "pattern_type" in p
            assert "description" in p
            assert "count" in p
            assert "estimated_dollars" in p
            assert "recommendation" in p


class TestEstimateFinancialImpact:

    def test_returns_dict_structure(self):
        changes = [
            {"pattern_type": "recurring", "description": "test", "count": 5,
             "estimated_dollars": 1000.0, "recommendation": "fix it"},
            {"pattern_type": "high_volume", "description": "test2", "count": 10,
             "estimated_dollars": 2500.0, "recommendation": "fix it too"},
        ]
        result = estimate_financial_impact(changes)
        assert result["total_preventable_dollars"] == 3500.0
        assert result["total_preventable_claims"] == 15
        assert "by_pattern_type" in result
        assert "top_opportunities" in result

    def test_empty_changes(self):
        result = estimate_financial_impact([])
        assert result["total_preventable_dollars"] == 0.0
        assert result["total_preventable_claims"] == 0


class TestGenerateReport:

    def test_report_is_string(self, mock_db):
        with patch("reporting.self_learning.DB_PATH", mock_db):
            report = generate_self_learning_report()
        assert isinstance(report, str)
        assert len(report) > 100

    def test_report_has_sections(self, mock_db):
        with patch("reporting.self_learning.DB_PATH", mock_db):
            report = generate_self_learning_report()
        assert "SECTION 1" in report
        assert "SECTION 2" in report
        assert "SECTION 3" in report
        assert "SECTION 4" in report
        assert "SECTION 5" in report
        assert "SECTION 6" in report

    def test_report_has_header(self, mock_db):
        with patch("reporting.self_learning.DB_PATH", mock_db):
            report = generate_self_learning_report()
        assert "SELF-LEARNING REPORT" in report
        assert "LCI CLAIMS AUTOMATION" in report


class TestEmailReport:

    def test_email_fails_without_credentials(self):
        with patch.dict(os.environ, {"AUTOMATION_EMAIL": "", "AUTOMATION_EMAIL_PASSWORD": ""}):
            result = email_report("test report")
        assert result is False

    @patch("reporting.self_learning.smtplib.SMTP")
    def test_email_sends_with_credentials(self, mock_smtp):
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp.return_value.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {
            "AUTOMATION_EMAIL": "test@example.com",
            "AUTOMATION_EMAIL_PASSWORD": "testpass",
        }):
            result = email_report("test report content")

        assert result is True
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("test@example.com", "testpass")
        mock_server.sendmail.assert_called_once()

    @patch("reporting.self_learning.smtplib.SMTP")
    def test_email_sends_to_correct_recipients(self, mock_smtp):
        mock_server = MagicMock()
        mock_smtp.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp.return_value.__exit__ = MagicMock(return_value=False)

        with patch.dict(os.environ, {
            "AUTOMATION_EMAIL": "auto@test.com",
            "AUTOMATION_EMAIL_PASSWORD": "pass",
        }):
            email_report("report")

        call_args = mock_server.sendmail.call_args
        recipients = call_args[0][1]
        assert "ss@lifeconsultantsinc.org" in recipients
        assert "nm@lifeconsultantsinc.org" in recipients
