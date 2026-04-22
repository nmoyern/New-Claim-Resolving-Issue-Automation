"""
reporting/weekly_report.py
----------------------------
Weekly performance report delivered to Nicholas Moyer via ClickUp.

Includes:
  - Autonomous resolution rate + trends
  - Dollars recovered / written off / outstanding
  - Success by action type and MCO
  - Trending denial patterns
  - Numbered recommendations for approval/delay/deny

The automation does NOT make changes based on recommendations —
Nicholas must approve each one by number in a ClickUp comment.
"""
from __future__ import annotations

import os
from datetime import date, datetime, timedelta

from actions.clickup_tasks import (
    ClickUpTaskCreator,
    PRIORITY_NORMAL,
    MEMBER_NICHOLAS,
    _next_business_day,
)
from reporting.outcome_tracker import (
    get_autonomous_rate,
    get_success_by_action,
    get_success_by_mco,
    get_trending_denials,
    update_pending_outcomes,
    update_denial_patterns,
)
from logging_utils.logger import get_logger

logger = get_logger("weekly_report")

REPORT_TASK_NAME_PREFIX = "Weekly Claims Automation Report"


async def generate_and_deliver_weekly_report() -> str | None:
    """Generate the weekly report and create a ClickUp task for Nicholas.

    Returns the ClickUp task ID or None.
    """
    # First, update outcome data
    logger.info("Updating pending outcomes before report...")
    update_result = update_pending_outcomes()
    update_denial_patterns()
    logger.info("Outcome data refreshed", **update_result)

    # Generate report sections
    today = date.today()
    period_start = today - timedelta(days=7)
    rate_7d = get_autonomous_rate(days=7)
    rate_30d = get_autonomous_rate(days=30)
    by_action = get_success_by_action(days=7)
    by_mco = get_success_by_mco(days=7)
    patterns = get_trending_denials(min_occurrences=3)

    # Build the report
    report = _build_report(
        today, period_start, rate_7d, rate_30d,
        by_action, by_mco, patterns,
    )

    # Build numbered recommendations
    recommendations = _build_recommendations(
        rate_7d, rate_30d, by_action, by_mco, patterns,
    )

    # Combine into ClickUp task description
    description = (
        f"{report}\n\n"
        f"{'=' * 50}\n"
        f"RECOMMENDATIONS\n"
        f"{'=' * 50}\n\n"
        f"{recommendations}\n\n"
        f"--- RESPOND BELOW ---\n"
        f"To approve a recommendation, comment with its number:\n"
        f"  Approve: 1, 3, 5\n"
        f"  Delay: 2 (reason)\n"
        f"  Deny: 4 (reason)\n\n"
        f"The automation will NOT make any changes until you approve.\n"
        f"Mark this task as Complete when done reviewing."
    )

    # Create ClickUp task assigned to Nicholas
    tc = ClickUpTaskCreator()
    task_id = await tc.create_task(
        list_id=tc.list_id,
        name=f"{REPORT_TASK_NAME_PREFIX} — {today.strftime('%m/%d/%Y')}",
        description=description,
        assignees=[MEMBER_NICHOLAS],
        due_date=_next_business_day(),
        priority=PRIORITY_NORMAL,
    )

    if task_id:
        logger.info(
            "Weekly report delivered",
            task_id=task_id,
            date=today.isoformat(),
        )
    return task_id


def _build_report(
    today, period_start, rate_7d, rate_30d,
    by_action, by_mco, patterns,
) -> str:
    lines = [
        f"CLAIMS AUTOMATION PERFORMANCE REPORT",
        f"Week of {period_start.strftime('%m/%d/%Y')} - {today.strftime('%m/%d/%Y')}",
        "",
        "=" * 50,
        "KEY METRICS",
        "=" * 50,
        "",
        f"Claims processed (7 days): {rate_7d['total_claims']}",
        f"Autonomous resolution rate: {rate_7d['autonomous_rate']}",
        f"  (30-day rate: {rate_30d['autonomous_rate']})",
        "",
        f"Autonomous (no human): {rate_7d['autonomous_actions']}",
        f"Human intervention: {rate_7d['human_actions']}",
        f"Auto-resolved & paid: {rate_7d['auto_paid']}",
        "",
        f"Dollars recovered: ${rate_7d['dollars_recovered']:,.2f}",
        f"Dollars written off: ${rate_7d['dollars_written_off']:,.2f}",
        f"Dollars outstanding: ${rate_7d['dollars_outstanding']:,.2f}",
    ]

    # Trend comparison
    if rate_30d["total_claims"] > 0 and rate_7d["total_claims"] > 0:
        try:
            rate_7 = float(rate_7d["autonomous_rate"].replace("%", ""))
            rate_30 = float(rate_30d["autonomous_rate"].replace("%", ""))
            delta = rate_7 - rate_30
            trend = "improving" if delta > 0 else "declining" if delta < 0 else "stable"
            lines.append(
                f"\nAutonomous rate trend: {trend} "
                f"({'+' if delta >= 0 else ''}{delta:.1f}% vs 30-day avg)"
            )
        except (ValueError, AttributeError):
            pass

    # By action type
    if by_action:
        lines.extend([
            "",
            "=" * 50,
            "BY ACTION TYPE",
            "=" * 50,
            "",
        ])
        for a in by_action:
            lines.append(
                f"  {a['action']}: {a['total']} claims, "
                f"{a['success_rate']} paid, "
                f"avg {a['avg_resolution_days']} days"
            )

    # By MCO
    if by_mco:
        lines.extend([
            "",
            "=" * 50,
            "BY MCO",
            "=" * 50,
            "",
        ])
        for m in by_mco:
            lines.append(
                f"  {m['mco']}: {m['total']} claims, "
                f"{m['paid']} paid (${m['recovered']:,.2f}), "
                f"{m['auto_rate']} autonomous"
            )

    # Trending denial patterns
    if patterns:
        lines.extend([
            "",
            "=" * 50,
            "TRENDING DENIAL PATTERNS",
            "=" * 50,
            "",
        ])
        for p in patterns[:10]:
            auto_rate = (
                f"{p['auto_resolved_count'] / p['occurrence_count'] * 100:.0f}%"
                if p["occurrence_count"] > 0 else "N/A"
            )
            lines.append(
                f"  {p['entity_key']}/{p['mco']}/{p['denial_code']}: "
                f"{p['occurrence_count']}x total, "
                f"auto-resolved: {auto_rate}, "
                f"write-offs: {p['write_off_count']}"
            )
            if p.get("recommendation"):
                lines.append(f"    Insight: {p['recommendation']}")

    return "\n".join(lines)


def _build_recommendations(
    rate_7d, rate_30d, by_action, by_mco, patterns,
) -> str:
    recs = []
    rec_num = 1

    # Recommendation: patterns with high write-off rates
    for p in patterns:
        if p["occurrence_count"] >= 5 and p["write_off_count"] > p["occurrence_count"] * 0.5:
            recs.append(
                f"{rec_num}. [INVESTIGATE] {p['entity_key']}/{p['mco']}/"
                f"{p['denial_code']} has {p['write_off_count']} write-offs "
                f"out of {p['occurrence_count']} occurrences. "
                f"Root cause analysis may prevent future denials.\n"
                f"   Impact: ${p['total_billed'] - p['total_recovered']:,.2f} lost"
            )
            rec_num += 1

    # Recommendation: patterns with low auto-resolve rate
    for p in patterns:
        if (
            p["occurrence_count"] >= 5
            and p["auto_resolved_count"] < p["occurrence_count"] * 0.3
            and p["human_resolved_count"] > p["auto_resolved_count"]
        ):
            recs.append(
                f"{rec_num}. [AUTOMATE] {p['entity_key']}/{p['mco']}/"
                f"{p['denial_code']} is mostly human-resolved "
                f"({p['human_resolved_count']} human vs "
                f"{p['auto_resolved_count']} auto). "
                f"Review for automation opportunity."
            )
            rec_num += 1

    # Recommendation: MCOs with low autonomous rates
    for m in by_mco:
        try:
            auto_pct = float(m["auto_rate"].replace("%", ""))
            if auto_pct < 50 and m["total"] >= 5:
                recs.append(
                    f"{rec_num}. [IMPROVE] {m['mco']} has {m['auto_rate']} "
                    f"autonomous rate ({m['total']} claims). "
                    f"May need additional API integration or rule tuning."
                )
                rec_num += 1
        except (ValueError, AttributeError):
            pass

    # Recommendation: actions with low success rates
    for a in by_action:
        try:
            success_pct = float(a["success_rate"].replace("%", ""))
            if success_pct < 30 and a["total"] >= 5:
                recs.append(
                    f"{rec_num}. [REVIEW] Action '{a['action']}' has "
                    f"{a['success_rate']} success rate over {a['total']} "
                    f"claims. Consider alternative approach."
                )
                rec_num += 1
        except (ValueError, AttributeError):
            pass

    # Recommendation: if autonomous rate is declining
    try:
        rate_7 = float(rate_7d["autonomous_rate"].replace("%", ""))
        rate_30 = float(rate_30d["autonomous_rate"].replace("%", ""))
        if rate_7 < rate_30 - 5 and rate_7d["total_claims"] >= 10:
            recs.append(
                f"{rec_num}. [ALERT] Autonomous rate dropped from "
                f"{rate_30d['autonomous_rate']} (30-day) to "
                f"{rate_7d['autonomous_rate']} (7-day). "
                f"New denial patterns may be emerging."
            )
            rec_num += 1
    except (ValueError, AttributeError):
        pass

    if not recs:
        return "No recommendations this week — automation is performing within normal parameters."

    return "\n\n".join(recs)
