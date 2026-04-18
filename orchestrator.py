"""
orchestrator.py
---------------
Main automation orchestrator for billed claim rejection/denial work.

This new repo is intentionally narrower than the old automation:
  1. Download/stage/post ERAs so paid claims can clear
  2. Pull claims that were billed and came back rejected/denied
  3. Ask the payer API what the payer currently says
     - United Healthcare → Optum
     - all other MCOs → Availity
  4. Route only the claims that still need work
  5. Log results to ClickUp daily task + Google Sheets
  6. Save human review queue

Usage:
  python orchestrator.py                    # Run now
  python orchestrator.py --dry-run          # Simulate without submitting
  python orchestrator.py --schedule         # Run on daily schedule (7am M-F)
  python orchestrator.py --action era       # Only run ERA upload/posting
  python orchestrator.py --action correct   # Only run corrections
"""
from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import date, datetime
from typing import List, Optional

from config.models import (
    Claim,
    ClaimStatus,
    DailyRunSummary,
    DenialCode,
    ERA,
    ResolutionAction,
    ResolutionResult,
)
from config.settings import DRY_RUN, MAX_CLAIMS_PER_RUN
from decision_tree.router import ClaimRouter, get_todays_primary_actions
from actions.handlers import (
    handle_correct_and_resubmit,
    handle_coverage_terminated,
    handle_era_upload,
    handle_lauris_fax_verify,
    handle_lauris_fix_company,
    handle_mco_auth_check,
    handle_phone_call_flag,
    handle_reconsideration,
    handle_appeal,
    handle_write_off,
    flush_phone_call_queue,
    flush_writeoff_approval_queue,
)
from exceptions.human_review_queue import HumanReviewQueue
from logging_utils.logger import ClickUpLogger, SheetsLogger, get_logger, setup_logging
from sources.claimmd import ClaimMDSession
from sources.claimmd_api import ClaimMDAPI
from reporting.gap_report import GapReporter, GapCategory
from reporting.self_learning import (
    check_self_learning_approvals,
    increment_run_count,
    should_generate_report,
    generate_self_learning_report,
    email_report,
)
from actions.era_manager import download_and_stage_eras
from actions.era_poster import post_pending_eras
from actions.pre_billing_check import run_pre_billing_checks
from sources.lauris_xml import (
    fetch_outstanding_claims,
    enrich_with_claimmd_notes,
    fetch_claimmd_notes_and_denials,
    is_claim_in_ar,
    generate_unified_excel,
)
from sources.payer_inquiry import (
    attach_payer_api_details_to_claim,
    check_payer_claim_status,
    is_billed_rejected_or_denied,
)
from sources.lauris_demographics import enrich_claims_with_demographics
from actions.auth_followup_tasks import (
    create_missing_auth_clickup_tasks,
    needs_authorization_before_resubmission,
)
from reporting.end_of_run_report import build_ar_lookup, generate_end_of_run_report

logger = get_logger("orchestrator")
router = ClaimRouter()
clickup = ClickUpLogger()
sheets = SheetsLogger()


# ---------------------------------------------------------------------------
# Action dispatcher
# ---------------------------------------------------------------------------

async def dispatch(claim: Claim, action: ResolutionAction) -> ResolutionResult:
    """Execute the correct handler for a given action."""
    handlers = {
        ResolutionAction.CORRECT_AND_RESUBMIT:  lambda c: handle_correct_and_resubmit(c),
        ResolutionAction.RECONSIDERATION:        lambda c: handle_reconsideration(c),
        ResolutionAction.MCO_PORTAL_AUTH_CHECK:  lambda c: handle_mco_auth_check(c),
        ResolutionAction.LAURIS_FIX_COMPANY:     lambda c: handle_lauris_fix_company(c),
        ResolutionAction.REPROCESS_LAURIS:       lambda c: handle_correct_and_resubmit(c),
        ResolutionAction.WRITE_OFF:              lambda c: handle_write_off(c),
        ResolutionAction.APPEAL_STEP3:           lambda c: handle_appeal(c),
        ResolutionAction.PHONE_CALL_THURSDAY:    lambda c: handle_phone_call_flag(c),
        ResolutionAction.HUMAN_REVIEW:           lambda c: _smart_human_review(c),
        ResolutionAction.SKIP:                   lambda c: _skip(c),
    }
    handler = handlers.get(action)
    if handler:
        return await handler(claim)
    return ResolutionResult(
        claim=claim,
        action_taken=action,
        success=False,
        needs_human=True,
        human_reason=f"No handler for action: {action.value}",
    )


async def _flag_human(claim: Claim, reason: str) -> ResolutionResult:
    return ResolutionResult(
        claim=claim,
        action_taken=ResolutionAction.HUMAN_REVIEW,
        success=False,
        needs_human=True,
        human_reason=reason,
    )


async def _smart_human_review(claim: Claim) -> ResolutionResult:
    """Route human review claims through specialized handlers when possible."""
    if claim.denial_codes and DenialCode.COVERAGE_TERMINATED in claim.denial_codes:
        return await handle_coverage_terminated(claim)
    return await _flag_human(claim, "Routed to human review by decision tree")


async def _skip(claim: Claim) -> ResolutionResult:
    return ResolutionResult(
        claim=claim,
        action_taken=ResolutionAction.SKIP,
        success=True,
        note_written="Skipped — claim too new or in progress.",
    )


# ---------------------------------------------------------------------------
# Main daily run
# ---------------------------------------------------------------------------

async def run_daily(
    force_actions: Optional[List[ResolutionAction]] = None,
    max_claims: int = MAX_CLAIMS_PER_RUN,
    full_pull: bool = False,
) -> DailyRunSummary:
    """
    Narrow daily automation run:
      1. Run the full ERA download/stage/posting workflow
      2. Get billed claims that were rejected/denied
      3. Confirm payer status through Optum/Availity APIs
      4. Route and execute only claims that still need work
      5. Log results
    """
    summary = DailyRunSummary()
    human_queue = HumanReviewQueue()
    gap_reporter = GapReporter()
    todays_actions = force_actions or get_todays_primary_actions()
    ar_lookup = {}

    logger.info(
        "Starting billed rejected/denied claims automation",
        date=str(date.today()),
        dry_run=DRY_RUN,
        actions=[a.value for a in todays_actions],
    )

    try:
        outstanding_claims = fetch_outstanding_claims(lookback_days=365)
        ar_lookup = build_ar_lookup(outstanding_claims)
        logger.info(
            "Outstanding/balance due lookup loaded",
            entries=len(ar_lookup),
        )
    except Exception as e:
        logger.warning(
            "Outstanding/balance due lookup failed",
            error=str(e),
        )

    # -------------------------------------------------------
    # Step 1: Full ERA download/stage/posting workflow
    # -------------------------------------------------------
    if ResolutionAction.ERA_UPLOAD in todays_actions:
        logger.info("Step 1a: ERA download and staging")
        try:
            era_counts = await download_and_stage_eras()
            summary.eras_uploaded = era_counts.get("staged", 0)
            logger.info("ERA staging complete", **era_counts)
        except Exception as e:
            logger.error("ERA staging failed — continuing with claim processing", error=str(e))
            summary.errors.append(f"ERA staging failed: {e}")

        logger.info("Step 1b: Posting ERAs to Lauris via EDI Results")
        try:
            era_post_result = await post_pending_eras()
            if era_post_result.get("posted", 0) > 0:
                summary.eras_uploaded += era_post_result["posted"]
            logger.info("ERA posting complete", **{
                k: v for k, v in era_post_result.items()
                if k not in ("posted_files", "irregular_files")
            })
        except Exception as e:
            logger.error("ERA posting failed — continuing with claim processing", error=str(e))
            summary.errors.append(f"ERA posting failed: {e}")

    if force_actions == [ResolutionAction.ERA_UPLOAD]:
        run_report = generate_end_of_run_report(summary, ar_lookup=ar_lookup, clickup_task_map={})
        await _post_summary(summary, human_queue, run_report)
        gap_reporter.close()
        return summary

    # -------------------------------------------------------
    # Step 2: Get rejected/denied claims from Claim.MD
    # -------------------------------------------------------
    logger.info("Step 2: Fetching rejected/denied claims from Claim.MD")
    claims: List[Claim] = []
    try:
        api = ClaimMDAPI()
        if api.key:
            logger.info("Using Claim.MD API to fetch claims", full_pull=full_pull)
            claims = await api.get_denied_claims(full_pull=full_pull)
        else:
            logger.info("No API key — falling back to browser session")
            async with ClaimMDSession() as claimmd:
                claims = await claimmd.get_denied_claims()
    except Exception as e:
        logger.error("Failed to fetch claims from Claim.MD", error=str(e))
        summary.errors.append(f"Claim.MD fetch failed: {e}")

    fetched_count = len(claims)
    claims = [c for c in claims if is_billed_rejected_or_denied(c)]
    try:
        claims = enrich_claims_with_demographics(claims)
        logger.info("Claims enriched with Lauris demographics where available")
    except Exception as e:
        logger.warning("Lauris demographics enrichment failed", error=str(e))
    summary.claims_at_start = len(claims)
    logger.info(
        "Filtered to billed rejected/denied claims",
        fetched=fetched_count,
        actionable=len(claims),
    )

    if not claims:
        run_report = generate_end_of_run_report(summary, ar_lookup=ar_lookup, clickup_task_map={})
        await _post_summary(summary, human_queue, run_report)
        gap_reporter.close()
        return summary

    # -------------------------------------------------------
    # Step 3: Ask payer APIs, then route and execute each claim
    # -------------------------------------------------------
    processed = 0
    missing_auth_claims: List[Claim] = []
    for claim in claims[:max_claims]:
        payer_result = await check_payer_claim_status(claim)
        logger.info(
            "Payer API check complete",
            claim_id=claim.claim_id,
            mco=claim.mco.value,
            gateway=payer_result.gateway,
            bucket=payer_result.bucket,
            should_process=payer_result.should_process,
        )
        attach_payer_api_details_to_claim(claim, payer_result)
        if not payer_result.should_process:
            result = ResolutionResult(
                claim=claim,
                action_taken=ResolutionAction.SKIP,
                success=True,
                note_written=payer_result.reason,
            )
            summary.claims_completed += 1
            summary.results.append(result)
            _log_to_sheets(claim, result)
            continue

        action, reason = router.route(claim)
        if needs_authorization_before_resubmission(claim):
            missing_auth_claims.append(claim)

        # Skip if this action isn't in today's action list
        if action not in todays_actions and action not in {
            ResolutionAction.SKIP, ResolutionAction.HUMAN_REVIEW
        }:
            logger.debug("Skipping claim — action not in run list", action=action.value)
            continue

        logger.info(
            "Processing claim",
            claim_id=claim.claim_id,
            client=claim.client_name,
            mco=claim.mco.value,
            action=action.value,
            reason=reason,
        )

        try:
            result = await dispatch(claim, action)
        except Exception as e:
            logger.error("Claim processing error", claim_id=claim.claim_id, error=str(e))
            result = ResolutionResult(
                claim=claim,
                action_taken=action,
                success=False,
                needs_human=True,
                human_reason=f"Automation error: {str(e)[:120]}",
                error=str(e),
            )
            summary.errors.append(f"Claim {claim.claim_id}: {e}")

        # Tally results
        if result.action_taken == ResolutionAction.WRITE_OFF:
            summary.write_offs += 1
        elif result.action_taken == ResolutionAction.CORRECT_AND_RESUBMIT:
            summary.corrections_made += 1
        elif result.action_taken == ResolutionAction.RECONSIDERATION:
            summary.recons_submitted += 1
        elif result.action_taken == ResolutionAction.APPEAL_STEP3:
            summary.appeals_submitted += 1

        if result.needs_human:
            human_queue.add(result)
            summary.human_review_flags += 1
        elif result.success:
            summary.claims_completed += 1

        summary.results.append(result)
        _log_to_sheets(claim, result)

        # Log to gap report database for historical tracking
        gap_cat = _denial_to_gap_category(claim)
        gap_reporter.log_claim_action(
            claim_id=claim.claim_id,
            action=result.action_taken.value,
            result="success" if result.success else "failed",
            note=result.note_written[:200] if result.note_written else "",
            gap_category=gap_cat.value if gap_cat else "",
            dollar_amount=claim.billed_amount,
        )
        if gap_cat:
            gap_reporter.log_gap(
                claim_id=claim.claim_id,
                client_name=claim.client_name,
                mco=claim.mco.value,
                program=claim.program.value,
                denial_type="|".join(code.value for code in claim.denial_codes) if claim.denial_codes else "unknown",
                gap_category=gap_cat,
                dollar_amount=claim.billed_amount,
                resolution=result.action_taken.value,
                status="resolved" if result.success else ("write_off" if result.action_taken == ResolutionAction.WRITE_OFF else "pending"),
            )

        processed += 1

        # Small delay between claims to avoid hammering portals
        await asyncio.sleep(1.5)

    clickup_task_map: dict[str, str] = {}
    if missing_auth_claims:
        try:
            clickup_task_map = await create_missing_auth_clickup_tasks(missing_auth_claims)
        except Exception as e:
            logger.warning(
                "Failed to create grouped missing-auth ClickUp tasks",
                error=str(e),
            )

    # -------------------------------------------------------
    # Step 4: Keep human follow-up moving through ClickUp
    # -------------------------------------------------------
    try:
        from actions.clickup_poller import (
            poll_completed_tasks, check_open_task_comments,
        )
        poll_result = await poll_completed_tasks()
        if poll_result["tasks_completed"] > 0:
            logger.info(
                "ClickUp task responses processed",
                completed=poll_result["tasks_completed"],
                actions=poll_result["actions_taken"],
            )
        comment_result = await check_open_task_comments()
        if comment_result["responses_found"] > 0 or comment_result["follow_ups_sent"] > 0:
            logger.info(
                "ClickUp conversational follow-up",
                responses=comment_result["responses_found"],
                actions=comment_result["actions_taken"],
                follow_ups=comment_result["follow_ups_sent"],
                due_dates_restored=comment_result["due_dates_restored"],
                assignees_restored=comment_result["assignees_restored"],
            )
    except Exception as e:
        logger.warning("ClickUp polling failed", error=str(e))

    # -------------------------------------------------------
    # Step 5: Post summary
    # -------------------------------------------------------
    human_queue.save()
    run_report = generate_end_of_run_report(
        summary,
        ar_lookup=ar_lookup,
        clickup_task_map=clickup_task_map,
    )
    await _post_summary(summary, human_queue, run_report)

    gap_reporter.close()

    logger.info(
        "Daily run complete",
        completed=summary.claims_completed,
        human_flags=summary.human_review_flags,
        errors=len(summary.errors),
    )
    return summary


async def _post_summary(summary: DailyRunSummary, human_queue: HumanReviewQueue, run_report: dict | None = None):
    """Post daily comment to ClickUp + save human review queue."""
    comment = summary.to_clickup_comment()
    if human_queue.count > 0:
        comment += "\n\n" + human_queue.to_summary_text()
    if run_report:
        output = run_report.get("output", {})
        comment += (
            "\n\nDetailed run report:\n"
            f"- Markdown: {output.get('markdown_path', '')}\n"
            f"- JSON: {output.get('json_path', '')}\n"
            f"- Dropbox Markdown: {output.get('markdown_dropbox_path', '')}\n"
            f"- Dropbox JSON: {output.get('json_dropbox_path', '')}"
        )
    await clickup.post_comment(comment)


def _denial_to_gap_category(claim: Claim):
    """Map a claim's primary denial code to a gap category."""
    if not claim.denial_codes:
        return GapCategory.UNKNOWN
    code = claim.denial_codes[0]
    mapping = {
        DenialCode.NO_AUTH: GapCategory.AUTH_NEVER_SUBMITTED,
        DenialCode.AUTH_EXPIRED: GapCategory.AUTH_NOT_ENTERED_LAURIS,
        DenialCode.INVALID_ID: GapCategory.BILLING_WRONG_MEMBER_ID,
        DenialCode.INVALID_DOB: GapCategory.BILLING_WRONG_MEMBER_ID,
        DenialCode.INVALID_NPI: GapCategory.SYSTEM_CONFIGURATION,
        DenialCode.INVALID_DIAG: GapCategory.SYSTEM_CONFIGURATION,
        DenialCode.WRONG_BILLING_CO: GapCategory.BILLING_WRONG_PROGRAM,
        DenialCode.DUPLICATE: GapCategory.BILLING_DOUBLE_BILLING,
        DenialCode.NOT_ENROLLED: GapCategory.MCO_ERROR,
        DenialCode.TIMELY_FILING: GapCategory.BILLING_TIMELY_FILING,
        DenialCode.RURAL_RATE_REDUCTION: GapCategory.BILLING_INCORRECT_RATE,
        DenialCode.UNDERPAID: GapCategory.BILLING_INCORRECT_RATE,
        DenialCode.RECOUPMENT: GapCategory.MCO_ERROR,
        DenialCode.DIAGNOSIS_BLANK: GapCategory.SYSTEM_CONFIGURATION,
        DenialCode.MISSING_NPI_RENDERING: GapCategory.SYSTEM_CONFIGURATION,
        DenialCode.COVERAGE_TERMINATED: GapCategory.MCO_ERROR,
        DenialCode.EXCEEDED_UNITS: GapCategory.AUTH_NOT_ENTERED_LAURIS,
    }
    return mapping.get(code, GapCategory.UNKNOWN)


def _log_to_sheets(claim: Claim, result: ResolutionResult):
    """Log a claim action to the Claim Denial Calls Google Sheet."""
    try:
        sheets.log_claim_action(
            claim_id=claim.claim_id,
            client_name=claim.client_name,
            mco=claim.mco.value,
            action=result.action_taken.value,
            notes=result.note_written[:200] if result.note_written else result.human_reason[:200],
        )
    except Exception as e:
        logger.warning("Sheets logging failed", error=str(e))


# ---------------------------------------------------------------------------
# Scheduler
# ---------------------------------------------------------------------------

def start_scheduler():
    """Run on weekday mornings at 7:00 AM."""
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_daily,
        CronTrigger(day_of_week="mon-fri", hour=7, minute=0),
        id="daily_claims_run",
        name="LCI Daily Claims Automation",
        misfire_grace_time=3600,
    )
    scheduler.start()
    logger.info("Scheduler started — will run Monday-Friday at 7:00 AM")

    try:
        asyncio.get_event_loop().run_forever()
    except (KeyboardInterrupt, SystemExit):
        scheduler.shutdown()
        logger.info("Scheduler stopped")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(description="LCI Claims Automation")
    parser.add_argument("--dry-run", action="store_true", help="Log actions without executing")
    parser.add_argument("--schedule", action="store_true", help="Run on daily schedule")
    parser.add_argument(
        "--action",
        choices=["era", "correct", "recon", "appeal", "writeoff", "auth", "all", "today"],
        default="all",
        help="Run only a specific action type",
    )
    parser.add_argument("--max-claims", type=int, default=MAX_CLAIMS_PER_RUN)
    parser.add_argument("--full-pull", action="store_true",
                        help="Pull ALL claims from Claim.MD (not just new since last run)")
    return parser.parse_args()


ALL_ACTIONS = [
    ResolutionAction.ERA_UPLOAD,
    ResolutionAction.CORRECT_AND_RESUBMIT,
    ResolutionAction.RECONSIDERATION,
    ResolutionAction.MCO_PORTAL_AUTH_CHECK,
    ResolutionAction.LAURIS_FIX_COMPANY,
    ResolutionAction.REPROCESS_LAURIS,
    ResolutionAction.WRITE_OFF,
    ResolutionAction.APPEAL_STEP3,
    ResolutionAction.PHONE_CALL_THURSDAY,
    ResolutionAction.HUMAN_REVIEW,
    ResolutionAction.SKIP,
]

ACTION_MAP = {
    "era":      [ResolutionAction.ERA_UPLOAD],
    "correct":  [ResolutionAction.CORRECT_AND_RESUBMIT],
    "recon":    [ResolutionAction.RECONSIDERATION],
    "appeal":   [ResolutionAction.APPEAL_STEP3],
    "writeoff": [ResolutionAction.WRITE_OFF],
    "auth":     [ResolutionAction.MCO_PORTAL_AUTH_CHECK],
    "all":      ALL_ACTIONS,  # Run everything regardless of day
    "today":    None,  # None = use today's schedule
}


if __name__ == "__main__":
    setup_logging()
    args = parse_args()

    if args.dry_run:
        import os
        os.environ["DRY_RUN"] = "true"
        logger.info("DRY RUN MODE — no changes will be submitted")

    if args.schedule:
        start_scheduler()
    else:
        force_actions = ACTION_MAP.get(args.action)
        asyncio.run(run_daily(
            force_actions=force_actions,
            max_claims=args.max_claims,
            full_pull=args.full_pull,
        ))
