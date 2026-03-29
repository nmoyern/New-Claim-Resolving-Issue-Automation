"""
reporting/gap_report.py
-----------------------
Gap reporting and historical trend analysis for LCI claims automation.

Every denial resolved generates a gap report entry to identify patterns
across claims, departments, and staff members for training and process improvement.

Uses SQLite for persistent local storage.
"""
from __future__ import annotations

import enum
import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import List, Optional, Tuple

from logging_utils.logger import get_logger

logger = get_logger("gap_report")

DB_DIR = Path("./data")
DB_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DB_DIR / "claims_history.db"


class GapCategory(str, enum.Enum):
    AUTH_NEVER_SUBMITTED = "AUTH — Never Submitted"
    AUTH_WRONG_MCO = "AUTH — Submitted to Wrong MCO"
    AUTH_NOT_SAVED_DROPBOX = "AUTH — Submitted but Not Saved to Dropbox"
    AUTH_FAX_NOT_CONFIRMED = "AUTH — Fax Not Confirmed"
    AUTH_NOT_ENTERED_LAURIS = "AUTH — Not Entered in Lauris After Approval"
    BILLING_WRONG_PROGRAM = "BILLING — Wrong Program / Billing Company"
    BILLING_WRONG_MEMBER_ID = "BILLING — Wrong Member ID / DOB / NPI"
    BILLING_TIMELY_FILING = "BILLING — Not Billed Within Timely Filing Window"
    BILLING_DOUBLE_BILLING = "BILLING — Double Billing"
    BILLING_INCORRECT_RATE = "BILLING — Incorrect Rate / Units"
    DOCUMENTATION_NOTE_MISSING = "DOCUMENTATION — Note Incomplete or Missing"
    DOCUMENTATION_ERA_NOT_UPLOADED = "DOCUMENTATION — ERA Not Uploaded"
    DOCUMENTATION_AUTH_NOT_SAVED = "DOCUMENTATION — Auth Not Saved Post-Approval"
    MCO_ERROR = "MCO ERROR"
    SYSTEM_CONFIGURATION = "SYSTEM / CONFIGURATION"
    UNKNOWN = "UNKNOWN"


class GapReporter:
    """Manages gap report entries, historical tracking, and trend analysis."""

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
        conn = self._get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS gap_report (
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

            CREATE TABLE IF NOT EXISTS claim_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id TEXT NOT NULL,
                date TEXT NOT NULL,
                action_taken TEXT NOT NULL,
                result TEXT NOT NULL,
                note_written TEXT DEFAULT '',
                gap_category TEXT DEFAULT '',
                dollar_amount REAL DEFAULT 0.0,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_gap_claim ON gap_report(claim_id);
            CREATE INDEX IF NOT EXISTS idx_gap_client ON gap_report(client_name);
            CREATE INDEX IF NOT EXISTS idx_gap_staff ON gap_report(staff_responsible);
            CREATE INDEX IF NOT EXISTS idx_gap_category ON gap_report(gap_category);
            CREATE INDEX IF NOT EXISTS idx_gap_date ON gap_report(date);
            CREATE INDEX IF NOT EXISTS idx_history_claim ON claim_history(claim_id);
            CREATE INDEX IF NOT EXISTS idx_history_date ON claim_history(date);

            CREATE TABLE IF NOT EXISTS new_denial_patterns (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                claim_id TEXT NOT NULL,
                raw_text TEXT NOT NULL,
                date TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_patterns_date ON new_denial_patterns(date);
        """)
        # Add denial_raw column if it doesn't exist yet
        try:
            conn.execute(
                "ALTER TABLE claim_history "
                "ADD COLUMN denial_raw TEXT DEFAULT ''"
            )
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists
        conn.commit()

    def log_gap(
        self,
        claim_id: str,
        client_name: str,
        mco: str,
        program: str,
        denial_type: str,
        gap_category: GapCategory,
        staff_responsible: str = "",
        dollar_amount: float = 0.0,
        resolution: str = "",
        lauris_fix: str = "",
        status: str = "pending",
    ) -> int:
        """Log a gap report entry. Returns the row ID."""
        recurrence = self.check_recurrence(client_name, gap_category)
        conn = self._get_conn()
        cur = conn.execute(
            """INSERT INTO gap_report
               (date, claim_id, client_name, mco, program, denial_type,
                gap_category, staff_responsible, dollar_amount, resolution,
                lauris_fix, status, recurrence_flag, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                date.today().isoformat(), claim_id, client_name, mco, program,
                denial_type, gap_category.value, staff_responsible, dollar_amount,
                resolution, lauris_fix, status, int(recurrence),
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        logger.info(
            "Gap logged",
            claim_id=claim_id,
            gap_category=gap_category.value,
            dollar_amount=dollar_amount,
            recurrence=recurrence,
        )
        return cur.lastrowid

    def log_claim_action(
        self,
        claim_id: str,
        action: str,
        result: str,
        note: str = "",
        gap_category: str = "",
        dollar_amount: float = 0.0,
    ) -> int:
        """Log a claim action to the history table."""
        conn = self._get_conn()
        cur = conn.execute(
            """INSERT INTO claim_history
               (claim_id, date, action_taken, result, note_written,
                gap_category, dollar_amount, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                claim_id, date.today().isoformat(), action, result,
                note, gap_category, dollar_amount, datetime.now().isoformat(),
            ),
        )
        conn.commit()
        return cur.lastrowid

    def check_recurrence(
        self, client_name: str, gap_category: GapCategory, days: int = 60
    ) -> bool:
        """Check if this gap type has appeared for this client within the last N days."""
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        conn = self._get_conn()
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM gap_report
               WHERE client_name = ? AND gap_category = ? AND date >= ?""",
            (client_name, gap_category.value, cutoff),
        ).fetchone()
        return row["cnt"] > 0

    def check_staff_training_trigger(
        self, staff_name: str, gap_category: GapCategory,
        days: int = 30, threshold: int = 3,
    ) -> bool:
        """Check if a staff member has exceeded the training trigger threshold."""
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        conn = self._get_conn()
        row = conn.execute(
            """SELECT COUNT(*) as cnt FROM gap_report
               WHERE staff_responsible = ? AND gap_category = ? AND date >= ?""",
            (staff_name, gap_category.value, cutoff),
        ).fetchone()
        return row["cnt"] >= threshold

    def get_training_triggers(
        self, days: int = 30, threshold: int = 3,
    ) -> List[Tuple[str, str, int]]:
        """Return all (staff, gap_category, count) tuples that exceed threshold."""
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        conn = self._get_conn()
        rows = conn.execute(
            """SELECT staff_responsible, gap_category, COUNT(*) as cnt
               FROM gap_report
               WHERE staff_responsible != '' AND date >= ?
               GROUP BY staff_responsible, gap_category
               HAVING cnt >= ?
               ORDER BY cnt DESC""",
            (cutoff, threshold),
        ).fetchall()
        return [(r["staff_responsible"], r["gap_category"], r["cnt"]) for r in rows]

    def check_writeoff_threshold(self, threshold: float = 2000.0) -> bool:
        """Check if this week's write-offs exceed the dollar threshold."""
        week_start = (date.today() - timedelta(days=date.today().weekday())).isoformat()
        conn = self._get_conn()
        row = conn.execute(
            """SELECT COALESCE(SUM(dollar_amount), 0) as total
               FROM gap_report WHERE status = 'write_off' AND date >= ?""",
            (week_start,),
        ).fetchone()
        return row["total"] >= threshold

    def get_weekly_trends(self) -> dict:
        """Get this week's trend data."""
        week_start = (date.today() - timedelta(days=date.today().weekday())).isoformat()
        conn = self._get_conn()

        total = conn.execute(
            "SELECT COUNT(*) as cnt FROM gap_report WHERE date >= ?", (week_start,)
        ).fetchone()["cnt"]

        writeoffs = conn.execute(
            """SELECT COUNT(*) as cnt, COALESCE(SUM(dollar_amount), 0) as total
               FROM gap_report WHERE status = 'write_off' AND date >= ?""",
            (week_start,),
        ).fetchone()

        by_category = conn.execute(
            """SELECT gap_category, COUNT(*) as cnt
               FROM gap_report WHERE date >= ?
               GROUP BY gap_category ORDER BY cnt DESC""",
            (week_start,),
        ).fetchall()

        by_mco = conn.execute(
            """SELECT mco, COUNT(*) as cnt
               FROM gap_report WHERE date >= ?
               GROUP BY mco ORDER BY cnt DESC""",
            (week_start,),
        ).fetchall()

        by_program = conn.execute(
            """SELECT program, COUNT(*) as cnt
               FROM gap_report WHERE date >= ?
               GROUP BY program ORDER BY cnt DESC""",
            (week_start,),
        ).fetchall()

        timely_filing = conn.execute(
            """SELECT COUNT(*) as cnt, COALESCE(SUM(dollar_amount), 0) as total
               FROM gap_report WHERE gap_category = ? AND date >= ?""",
            (GapCategory.BILLING_TIMELY_FILING.value, week_start),
        ).fetchone()

        dropbox_misses = conn.execute(
            """SELECT COUNT(*) as cnt FROM gap_report
               WHERE gap_category = ? AND date >= ?""",
            (GapCategory.AUTH_NOT_SAVED_DROPBOX.value, week_start),
        ).fetchone()["cnt"]

        recurring = conn.execute(
            """SELECT COUNT(*) as cnt FROM gap_report
               WHERE recurrence_flag = 1 AND date >= ?""",
            (week_start,),
        ).fetchone()["cnt"]

        return {
            "total_denials": total,
            "writeoff_count": writeoffs["cnt"],
            "writeoff_dollars": writeoffs["total"],
            "by_gap_category": {r["gap_category"]: r["cnt"] for r in by_category},
            "by_mco": {r["mco"]: r["cnt"] for r in by_mco},
            "by_program": {r["program"]: r["cnt"] for r in by_program},
            "timely_filing_count": timely_filing["cnt"],
            "timely_filing_dollars": timely_filing["total"],
            "dropbox_misses": dropbox_misses,
            "recurring_clients": recurring,
            "training_triggers": self.get_training_triggers(),
        }

    # ------------------------------------------------------------------
    # Period metrics helper
    # ------------------------------------------------------------------

    def _period_metrics(self, start_iso: str, end_iso: Optional[str] = None) -> dict:
        """Get aggregate metrics for a date range."""
        conn = self._get_conn()
        where = "date >= ?"
        params: list = [start_iso]
        if end_iso:
            where += " AND date < ?"
            params.append(end_iso)

        totals = conn.execute(
            f"""SELECT
                    COUNT(*) as denial_count,
                    COALESCE(SUM(dollar_amount), 0) as denial_dollars,
                    SUM(CASE WHEN status = 'write_off' THEN 1 ELSE 0 END) as writeoff_count,
                    COALESCE(SUM(CASE WHEN status = 'write_off' THEN dollar_amount ELSE 0 END), 0) as writeoff_dollars,
                    SUM(CASE WHEN status = 'resolved' THEN 1 ELSE 0 END) as resolved_count,
                    COALESCE(SUM(CASE WHEN status = 'resolved' THEN dollar_amount ELSE 0 END), 0) as resolved_dollars,
                    SUM(CASE WHEN recurrence_flag = 1 THEN 1 ELSE 0 END) as recurrences
                FROM gap_report WHERE {where}""",
            params,
        ).fetchone()

        timely = conn.execute(
            f"SELECT COUNT(*) as cnt, COALESCE(SUM(dollar_amount), 0) as total FROM gap_report WHERE gap_category = ? AND {where}",
            [GapCategory.BILLING_TIMELY_FILING.value] + params,
        ).fetchone()

        # Resolution rate: resolved / (resolved + writeoff + human_review)
        resolved = totals["resolved_count"]
        total = totals["denial_count"]
        resolution_rate = round((resolved / total * 100), 1) if total > 0 else 0.0

        return {
            "denial_count": totals["denial_count"],
            "denial_dollars": round(totals["denial_dollars"], 2),
            "writeoff_count": totals["writeoff_count"],
            "writeoff_dollars": round(totals["writeoff_dollars"], 2),
            "resolved_count": resolved,
            "resolved_dollars": round(totals["resolved_dollars"], 2),
            "resolution_rate_pct": resolution_rate,
            "recurrences": totals["recurrences"],
            "timely_filing_count": timely["cnt"],
            "timely_filing_dollars": round(timely["total"], 2),
        }

    # ------------------------------------------------------------------
    # Multi-period trend comparison (30d / 60d / 90d / 6mo / 1yr)
    # ------------------------------------------------------------------

    TREND_PERIODS = {
        "this_week": 7,
        "last_week": 7,       # handled specially
        "30_days": 30,
        "60_days": 60,
        "90_days": 90,
        "6_months": 182,
        "1_year": 365,
    }

    def get_trend_comparison(self) -> dict:
        """Compare this week vs last week vs 4-week average."""
        today = date.today()
        this_week_start = (today - timedelta(days=today.weekday())).isoformat()
        last_week_start = (today - timedelta(days=today.weekday() + 7)).isoformat()

        return {
            "this_week": self._period_metrics(this_week_start),
            "last_week": self._period_metrics(last_week_start, this_week_start),
            "four_week_avg": self._averaged_metrics(28),
        }

    def get_performance_scorecard(self) -> dict:
        """
        Full performance scorecard across 30, 60, 90 days, 6 months, and 1 year.
        Shows whether we're getting better or worse at resolving claims.

        Returns dict with metrics for each period PLUS direction indicators.
        """
        today = date.today()
        periods = {}

        for label, days in [
            ("current_week", today.weekday()),
            ("30_days", 30),
            ("60_days", 60),
            ("90_days", 90),
            ("6_months", 182),
            ("1_year", 365),
        ]:
            start = (today - timedelta(days=days)).isoformat()
            metrics = self._period_metrics(start)
            # Normalize to per-week averages for fair comparison
            weeks = max(days / 7, 1)
            metrics["avg_denials_per_week"] = round(metrics["denial_count"] / weeks, 1)
            metrics["avg_writeoff_per_week"] = round(metrics["writeoff_dollars"] / weeks, 2)
            metrics["avg_resolved_per_week"] = round(metrics["resolved_count"] / weeks, 1)
            metrics["period_weeks"] = round(weeks, 1)
            periods[label] = metrics

        # Calculate direction: compare recent (30d) vs older (90d, 6mo)
        recent = periods.get("30_days", {})
        longer = periods.get("90_days", {})

        directions = {}
        if recent.get("avg_denials_per_week", 0) and longer.get("avg_denials_per_week", 0):
            r = recent["avg_denials_per_week"]
            l = longer["avg_denials_per_week"]
            if r < l * 0.9:
                directions["denials"] = "IMPROVING"
            elif r > l * 1.1:
                directions["denials"] = "WORSENING"
            else:
                directions["denials"] = "STABLE"

        if recent.get("resolution_rate_pct", 0) and longer.get("resolution_rate_pct", 0):
            r = recent["resolution_rate_pct"]
            l = longer["resolution_rate_pct"]
            if r > l + 5:
                directions["resolution_rate"] = "IMPROVING"
            elif r < l - 5:
                directions["resolution_rate"] = "WORSENING"
            else:
                directions["resolution_rate"] = "STABLE"

        if recent.get("avg_writeoff_per_week", 0) and longer.get("avg_writeoff_per_week", 0):
            r = recent["avg_writeoff_per_week"]
            l = longer["avg_writeoff_per_week"]
            if r < l * 0.9:
                directions["writeoffs"] = "IMPROVING"
            elif r > l * 1.1:
                directions["writeoffs"] = "WORSENING"
            else:
                directions["writeoffs"] = "STABLE"

        return {
            "periods": periods,
            "directions": directions,
            "generated": datetime.now().isoformat(),
        }

    def get_autonomous_corrections(self, days: int = 30) -> dict:
        """
        Count autonomous corrections that resolved claims without
        human intervention using the autonomous_corrections table.

        Tracks:
        - All auto-corrections (entity, NPI, member ID, MHSS rate,
          diagnosis, rendering NPI, resubmissions, reconsiderations)
        - Which corrections resulted in the claim being paid
        - Dollar amounts recovered
        """
        from reporting.autonomous_tracker import get_correction_stats

        stats = get_correction_stats(days=days)

        # Also get human review count for rate calculation
        conn = self._get_conn()
        cutoff = (date.today() - timedelta(days=days)).isoformat()
        try:
            human_needed = conn.execute(
                """SELECT COUNT(*) as cnt
                   FROM gap_report
                   WHERE status = 'human_review'
                   AND date >= ?""",
                (cutoff,),
            ).fetchone()
            human_cnt = human_needed["cnt"] if human_needed else 0
        except Exception:
            human_cnt = 0

        # Also get total claims processed for auto-fix rate
        try:
            total_processed = conn.execute(
                """SELECT COUNT(*) as cnt
                   FROM claim_history
                   WHERE date >= ?""",
                (cutoff,),
            ).fetchone()
            total_claims = total_processed["cnt"] if total_processed else 0
        except Exception:
            total_claims = 0

        total_corrected = stats["total_corrected"]
        total_resolved = stats["total_resolved"]

        total_interventions = total_corrected + human_cnt
        auto_rate = round(
            (total_corrected / total_interventions * 100), 1
        ) if total_interventions > 0 else 0.0

        auto_fix_rate = round(
            (total_corrected / total_claims * 100), 1
        ) if total_claims > 0 else 0.0

        human_intervention_rate = round(
            (human_cnt / (total_corrected + human_cnt) * 100), 1
        ) if (total_corrected + human_cnt) > 0 else 0.0

        return {
            "total_corrected": total_corrected,
            "total_resolved": total_resolved,
            "total_dollars_at_stake": stats["total_dollars_at_stake"],
            "dollars_recovered": stats["total_dollars_recovered"],
            "by_type": stats["by_type"],
            "claims_needing_human": human_cnt,
            "total_interventions": total_interventions,
            "autonomous_rate_pct": auto_rate,
            "auto_fix_rate_pct": auto_fix_rate,
            "resolution_rate_pct": stats["resolution_rate"],
            "human_intervention_rate_pct": human_intervention_rate,
            "total_claims_processed": total_claims,
        }

    def _averaged_metrics(self, days: int) -> dict:
        """Get metrics averaged per week over a period."""
        start = (date.today() - timedelta(days=days)).isoformat()
        raw = self._period_metrics(start)
        weeks = max(days / 7, 1)
        return {
            "denials": round(raw["denial_count"] / weeks, 1),
            "dollars": round(raw["denial_dollars"] / weeks, 2),
            "resolution_rate_pct": raw["resolution_rate_pct"],
        }

    def generate_weekly_report_text(self) -> str:
        """Generate a formatted weekly report for ClickUp posting."""
        trends = self.get_weekly_trends()
        comparison = self.get_trend_comparison()

        lines = [
            f"Weekly Gap Report — {date.today().strftime('%m/%d/%y')}",
            "",
            f"Total denials this week: {trends['total_denials']} "
            f"(last week: {comparison['last_week'].get('denial_count', comparison['last_week'].get('denials', 0))}, "
            f"4-week avg: {comparison['four_week_avg'].get('denials', 0)})",
            "",
            f"Write-offs: {trends['writeoff_count']} claims, "
            f"${trends['writeoff_dollars']:.2f}",
            "",
        ]

        if trends["by_gap_category"]:
            lines.append("By Gap Category:")
            for cat, cnt in trends["by_gap_category"].items():
                lines.append(f"  {cat}: {cnt}")
            lines.append("")

        if trends["by_mco"]:
            lines.append("By MCO:")
            for mco, cnt in trends["by_mco"].items():
                lines.append(f"  {mco}: {cnt}")
            lines.append("")

        if trends["timely_filing_count"] > 0:
            lines.append(
                f"TIMELY FILING (zero-tolerance): {trends['timely_filing_count']} claims, "
                f"${trends['timely_filing_dollars']:.2f} at risk"
            )

        if trends["dropbox_misses"] > 0:
            lines.append(
                f"Dropbox save failures: {trends['dropbox_misses']} this week"
            )

        if trends["recurring_clients"] > 0:
            lines.append(
                f"Recurring client denials (same type within 60 days): {trends['recurring_clients']}"
            )

        if trends["training_triggers"]:
            lines.append("")
            lines.append("TRAINING FLAGS:")
            for staff, cat, cnt in trends["training_triggers"]:
                lines.append(f"  {staff}: {cnt}x {cat} in 30 days")

        lines.append("")
        lines.append(f"#AUTO #{date.today().strftime('%m/%d/%y')}")
        return "\n".join(lines)

    def generate_performance_report_text(self) -> str:
        """
        Generate a full performance scorecard showing 30d/60d/90d/6mo/1yr trends.
        Shows whether we're getting better or worse at collecting funds.
        """
        sc = self.get_performance_scorecard()
        periods = sc["periods"]
        dirs = sc["directions"]

        def _arrow(direction: str) -> str:
            if direction == "IMPROVING":
                return "^ IMPROVING"
            elif direction == "WORSENING":
                return "v WORSENING"
            return "= STABLE"

        lines = [
            f"PERFORMANCE SCORECARD — {date.today().strftime('%m/%d/%y')}",
            "=" * 60,
            "",
            "OVERALL DIRECTION:",
        ]

        if dirs:
            for metric, direction in dirs.items():
                label = metric.replace("_", " ").title()
                lines.append(f"  {label}: {_arrow(direction)}")
        else:
            lines.append("  Not enough data yet — trends will appear after 30+ days of data.")

        lines.append("")
        lines.append("-" * 60)
        lines.append(f"{'Period':<15} {'Denials':>10} {'Resolved':>10} {'Rate':>8} {'Write-offs':>12} {'$/wk WO':>10}")
        lines.append("-" * 60)

        for label, display in [
            ("current_week", "This Week"),
            ("30_days", "30 Days"),
            ("60_days", "60 Days"),
            ("90_days", "90 Days"),
            ("6_months", "6 Months"),
            ("1_year", "1 Year"),
        ]:
            p = periods.get(label, {})
            if p.get("denial_count", 0) == 0 and label != "current_week":
                continue  # Skip empty periods
            lines.append(
                f"{display:<15} "
                f"{p.get('denial_count', 0):>10} "
                f"{p.get('resolved_count', 0):>10} "
                f"{p.get('resolution_rate_pct', 0):>7.1f}% "
                f"${p.get('writeoff_dollars', 0):>10,.2f} "
                f"${p.get('avg_writeoff_per_week', 0):>9,.2f}"
            )

        lines.append("-" * 60)
        lines.append("")
        lines.append("KEY METRICS (per week averages):")

        for label, display in [("30_days", "Last 30d"), ("90_days", "Last 90d"), ("6_months", "Last 6mo")]:
            p = periods.get(label, {})
            if p.get("denial_count", 0) == 0:
                continue
            lines.append(
                f"  {display}: {p.get('avg_denials_per_week', 0):.1f} denials/wk, "
                f"{p.get('avg_resolved_per_week', 0):.1f} resolved/wk, "
                f"${p.get('avg_writeoff_per_week', 0):,.2f} written off/wk"
            )

        # Timely filing zero-tolerance KPI
        for label in ["30_days", "90_days"]:
            p = periods.get(label, {})
            if p.get("timely_filing_count", 0) > 0:
                lines.append("")
                lines.append(
                    f"TIMELY FILING LOSSES ({label.replace('_', ' ')}): "
                    f"{p['timely_filing_count']} claims, ${p['timely_filing_dollars']:,.2f}"
                )

        # Autonomous corrections — how many issues resolved without humans
        lines.append("")
        lines.append("-" * 60)
        lines.append("AUTONOMOUS CORRECTIONS (no human intervention):")
        for period_days, label in [
            (7, "This Week"), (30, "30 Days"), (90, "90 Days"),
        ]:
            ac = self.get_autonomous_corrections(days=period_days)
            if ac["total_corrected"] > 0 or ac["claims_needing_human"] > 0:
                lines.append(
                    f"  {label}: {ac['total_corrected']} claims corrected, "
                    f"{ac['total_resolved']} resolved/paid "
                    f"(${ac['dollars_recovered']:,.2f} recovered)"
                )

        # By type breakdown (use 90-day window for meaningful data)
        ac90 = self.get_autonomous_corrections(days=90)
        if ac90["by_type"]:
            lines.append("")
            lines.append("  By Type:")
            type_labels = {
                "entity_fix": "entity_fix",
                "npi_fix": "npi_fix",
                "member_id_fix": "member_id_fix",
                "mhss_rate_fix": "mhss_rate_fix",
                "diagnosis_fix": "diagnosis_fix",
                "rendering_npi_added": "rendering_npi",
                "resubmitted": "resubmitted",
                "reconsideration_submitted": "recon_submitted",
            }
            for ctype, display in type_labels.items():
                data = ac90["by_type"].get(ctype, {})
                if data:
                    lines.append(
                        f"    {display + ':':<20} "
                        f"{data['corrected']} corrected, "
                        f"{data['resolved']} resolved"
                    )

        # Rates (use 30-day window)
        ac30 = self.get_autonomous_corrections(days=30)
        lines.append("")
        lines.append(
            f"  Auto-Fix Rate: {ac30['auto_fix_rate_pct']:.1f}% "
            f"(corrections / total claims processed)"
        )
        lines.append(
            f"  Resolution Rate: {ac30['resolution_rate_pct']:.1f}% "
            f"(resolved / total corrections)"
        )
        lines.append(
            f"  Human Intervention Rate: "
            f"{ac30['human_intervention_rate_pct']:.1f}% "
            f"(claims needing human review / total)"
        )

        lines.append("")
        lines.append(f"#AUTO #{date.today().strftime('%m/%d/%y')}")
        return "\n".join(lines)

    def store_raw_denial(self, claim_id: str, denial_raw: str):
        """Store the raw denial message text in claim_history."""
        conn = self._get_conn()
        try:
            conn.execute(
                "UPDATE claim_history SET denial_raw = ? "
                "WHERE claim_id = ? AND denial_raw = ''",
                (denial_raw[:500], claim_id),
            )
            conn.commit()
        except Exception:
            pass

    def log_new_pattern(self, claim_id: str, raw_text: str):
        """Log unrecognized denial text to new_denial_patterns table."""
        conn = self._get_conn()
        conn.execute(
            "INSERT INTO new_denial_patterns "
            "(claim_id, raw_text, date, created_at) "
            "VALUES (?, ?, ?, ?)",
            (
                claim_id,
                raw_text[:500],
                date.today().isoformat(),
                datetime.now().isoformat(),
            ),
        )
        conn.commit()
        logger.info(
            "New denial pattern logged",
            claim_id=claim_id,
            text=raw_text[:80],
        )

    def close(self):
        if self._conn:
            self._conn.close()
            self._conn = None
