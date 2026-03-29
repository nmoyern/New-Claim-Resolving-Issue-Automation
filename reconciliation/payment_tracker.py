"""
reconciliation/payment_tracker.py
----------------------------------
Tracks MCO payments and their bank reconciliation status.

Creates unique IDs for each unreconciled payment so the team
can email a code to mark payments as received.

SQLite database stores:
  - Payment records from ERAs (what MCOs say they paid)
  - Bank verification status (did it actually arrive?)
  - Unique tracking codes for email-based confirmation
  - Escalation dates and status
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional

from logging_utils.logger import get_logger

logger = get_logger("payment_tracker")

DB_PATH = Path("data/bank_reconciliation.db")


class PaymentTracker:
    """Manages payment tracking and reconciliation status.

    Simplified approach: look at incoming funds and determine the payor.
    No complex bank transaction matching — just track what came in and
    from whom.
    """

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = db_path
        self._conn: Optional[sqlite3.Connection] = None
        self._ensure_tables()

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(str(self.db_path))
            self._conn.row_factory = sqlite3.Row
        return self._conn

    def _ensure_tables(self):
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                unique_code TEXT UNIQUE NOT NULL,
                era_id TEXT,
                check_number TEXT,
                check_type TEXT DEFAULT 'eft',
                payer_name TEXT,
                payer_id TEXT,
                provider_npi TEXT,
                provider_name TEXT,
                program TEXT,
                bank_entity TEXT,
                paid_amount REAL,
                paid_date TEXT,
                received_date TEXT,
                pcn TEXT,
                status TEXT DEFAULT 'pending',
                bank_verified INTEGER DEFAULT 0,
                bank_verified_date TEXT,
                escalated INTEGER DEFAULT 0,
                escalation_date TEXT,
                clickup_task_id TEXT,
                marked_paid_by TEXT,
                marked_paid_date TEXT,
                write_off_amount REAL DEFAULT 0,
                write_off_reason TEXT DEFAULT '',
                notes TEXT DEFAULT '',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS email_commands (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email_id TEXT,
                sender TEXT,
                cc TEXT,
                subject TEXT,
                command_type TEXT,
                unique_code TEXT,
                paid_date TEXT,
                processed INTEGER DEFAULT 0,
                cancelled INTEGER DEFAULT 0,
                cancelled_by TEXT,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_pay_code ON payments(unique_code);
            CREATE INDEX IF NOT EXISTS idx_pay_status ON payments(status);
            CREATE INDEX IF NOT EXISTS idx_pay_check ON payments(check_number);
            CREATE INDEX IF NOT EXISTS idx_pay_era ON payments(era_id);
            CREATE INDEX IF NOT EXISTS idx_pay_pcn ON payments(pcn);
        """)
        conn.commit()

    def generate_unique_code(self, era_id: str, check_number: str, amount: float) -> str:
        """Generate a unique tracking code for a payment.
        Format: PAY-{short_hash}
        Example: PAY-A3F7B2
        """
        raw = f"{era_id}:{check_number}:{amount}:{date.today().isoformat()}"
        hash_hex = hashlib.sha256(raw.encode()).hexdigest()[:6].upper()
        return f"PAY-{hash_hex}"

    def add_payment(
        self,
        era_id: str,
        check_number: str,
        check_type: str,
        payer_name: str,
        payer_id: str,
        provider_npi: str,
        provider_name: str,
        program: str,
        paid_amount: float,
        paid_date: str,
        received_date: str = "",
        pcn: str = "",
    ) -> str:
        """Add a payment to track. Returns the unique tracking code."""
        # Determine which bank this goes to
        bank_entity = self._npi_to_bank(provider_npi)

        unique_code = self.generate_unique_code(era_id, check_number, paid_amount)

        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT INTO payments
                   (unique_code, era_id, check_number, check_type, payer_name,
                    payer_id, provider_npi, provider_name, program, bank_entity,
                    paid_amount, paid_date, received_date, pcn, status, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    unique_code, era_id, check_number, check_type, payer_name,
                    payer_id, provider_npi, provider_name, program, bank_entity,
                    paid_amount, paid_date, received_date, pcn,
                    datetime.now().isoformat(),
                ),
            )
            conn.commit()
            logger.info("Payment tracked",
                        code=unique_code, amount=paid_amount,
                        payer=payer_name, bank=bank_entity)
        except sqlite3.IntegrityError:
            # Already tracked
            row = conn.execute(
                "SELECT unique_code FROM payments WHERE era_id = ? AND check_number = ?",
                (era_id, check_number),
            ).fetchone()
            if row:
                return row["unique_code"]

        return unique_code

    def mark_paid(self, unique_code: str, paid_date: str = "", marked_by: str = "") -> bool:
        """Mark a payment as received/reconciled."""
        conn = self._get_conn()
        conn.execute(
            """UPDATE payments SET
                status = 'reconciled',
                bank_verified = 1,
                bank_verified_date = ?,
                marked_paid_by = ?,
                marked_paid_date = ?
               WHERE unique_code = ?""",
            (
                paid_date or date.today().isoformat(),
                marked_by,
                datetime.now().isoformat(),
                unique_code,
            ),
        )
        conn.commit()
        updated = conn.execute(
            "SELECT changes() as cnt"
        ).fetchone()["cnt"]
        if updated:
            logger.info("Payment marked as paid", code=unique_code)
        return updated > 0

    def mark_write_off(self, pcn: str, amount: float, reason: str) -> bool:
        """Mark a payment for write-off by PCN."""
        conn = self._get_conn()
        conn.execute(
            """UPDATE payments SET
                status = 'write_off',
                write_off_amount = ?,
                write_off_reason = ?
               WHERE pcn = ?""",
            (amount, reason, pcn),
        )
        conn.commit()
        return True

    def get_pending_payments(self) -> List[dict]:
        """Get all unreconciled payments."""
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM payments
               WHERE status = 'pending'
               ORDER BY paid_date ASC"""
        ).fetchall()
        return [dict(r) for r in rows]

    def get_overdue_payments(self, business_days: int = 5) -> List[dict]:
        """Get payments that haven't been verified within N business days."""
        cutoff = self._subtract_business_days(date.today(), business_days)
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM payments
               WHERE status = 'pending'
               AND paid_date <= ?
               AND escalated = 0
               ORDER BY paid_date ASC""",
            (cutoff.isoformat(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_escalation_needed(self, business_days: int = 7) -> List[dict]:
        """Get payments that need escalation (7+ business days unreconciled)."""
        cutoff = self._subtract_business_days(date.today(), business_days)
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT * FROM payments
               WHERE status = 'pending'
               AND paid_date <= ?
               AND escalated = 0
               ORDER BY paid_date ASC""",
            (cutoff.isoformat(),),
        ).fetchall()
        return [dict(r) for r in rows]

    def mark_escalated(self, unique_code: str, clickup_task_id: str = "") -> bool:
        """Mark a payment as escalated to human review."""
        conn = self._get_conn()
        conn.execute(
            """UPDATE payments SET
                escalated = 1,
                escalation_date = ?,
                clickup_task_id = ?
               WHERE unique_code = ?""",
            (datetime.now().isoformat(), clickup_task_id, unique_code),
        )
        conn.commit()
        return True

    def generate_reconciliation_report(self) -> str:
        """Generate the Claim Bank Reconciliation Report."""
        conn = self._get_conn()

        pending = conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(paid_amount), 0) as total FROM payments WHERE status = 'pending'"
        ).fetchone()

        reconciled = conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(paid_amount), 0) as total FROM payments WHERE status = 'reconciled'"
        ).fetchone()

        written_off = conn.execute(
            "SELECT COUNT(*) as cnt, COALESCE(SUM(write_off_amount), 0) as total FROM payments WHERE status = 'write_off'"
        ).fetchone()

        escalated = conn.execute(
            "SELECT COUNT(*) as cnt FROM payments WHERE escalated = 1 AND status = 'pending'"
        ).fetchone()

        # By bank
        by_bank = conn.execute(
            """SELECT bank_entity,
                      COUNT(*) as cnt,
                      SUM(CASE WHEN status = 'pending' THEN 1 ELSE 0 END) as pending,
                      SUM(CASE WHEN status = 'reconciled' THEN 1 ELSE 0 END) as reconciled,
                      COALESCE(SUM(CASE WHEN status = 'pending' THEN paid_amount ELSE 0 END), 0) as pending_amount
               FROM payments GROUP BY bank_entity"""
        ).fetchall()

        # Pending details
        pending_details = conn.execute(
            """SELECT unique_code, payer_name, provider_name, bank_entity,
                      paid_amount, paid_date, check_number, escalated
               FROM payments WHERE status = 'pending'
               ORDER BY paid_date ASC LIMIT 50"""
        ).fetchall()

        lines = [
            f"CLAIM BANK RECONCILIATION REPORT — {date.today().strftime('%m/%d/%y')}",
            "=" * 65,
            "",
            f"SUMMARY:",
            f"  Pending verification:  {pending['cnt']} payments, ${pending['total']:,.2f}",
            f"  Reconciled (verified): {reconciled['cnt']} payments, ${reconciled['total']:,.2f}",
            f"  Written off:           {written_off['cnt']} payments, ${written_off['total']:,.2f}",
            f"  Escalated to human:    {escalated['cnt']}",
            "",
            "BY BANK:",
        ]

        for row in by_bank:
            lines.append(
                f"  {row['bank_entity']}: "
                f"{row['pending']} pending (${row['pending_amount']:,.2f}), "
                f"{row['reconciled']} reconciled"
            )

        if pending_details:
            lines.append("")
            lines.append("-" * 65)
            lines.append("UNRECONCILED PAYMENTS:")
            lines.append(f"{'Code':<15} {'Payer':<20} {'Bank':<15} {'Amount':>10} {'Date':<12} {'Esc'}")
            lines.append("-" * 65)
            for p in pending_details:
                esc = "YES" if p["escalated"] else ""
                lines.append(
                    f"{p['unique_code']:<15} "
                    f"{p['payer_name'][:18]:<20} "
                    f"{p['bank_entity'][:13]:<15} "
                    f"${p['paid_amount']:>9,.2f} "
                    f"{p['paid_date']:<12} "
                    f"{esc}"
                )

        lines.append("")
        lines.append(
            "To mark a payment as received, email ea@lifeconsultantsinc.org "
            "with the unique code + '_PAID_DATE' in the subject line."
        )
        lines.append(f"#AUTO #{date.today().strftime('%m/%d/%y')}")
        return "\n".join(lines)

    @staticmethod
    def _npi_to_bank(npi: str) -> str:
        """Map provider NPI to bank entity."""
        mapping = {
            "1437871753": "Southern Bank (Mary's Home)",
            "1700297447": "Bank of America (NHCS)",
            "1306491592": "Wells Fargo (KJLN)",
        }
        return mapping.get(npi, "Unknown Bank")

    @staticmethod
    def _subtract_business_days(from_date: date, days: int) -> date:
        d = from_date
        subtracted = 0
        while subtracted < days:
            d -= timedelta(days=1)
            if d.weekday() < 5:
                subtracted += 1
        return d

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
