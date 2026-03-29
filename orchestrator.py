"""
orchestrator.py
---------------
Main automation orchestrator for LCI claims processing.

Runs the full daily/weekly claims workflow:
  1. Download new ERAs from Claim.MD → upload to Lauris
  2. Write off Rural Rate Reduction claims
  3. Pull denied/rejected claim list from Claim.MD
  4. Route each claim through the decision tree
  5. Execute the appropriate action (correct, reconsider, appeal, etc.)
  6. Log results to ClickUp daily task + Google Sheets
  7. Save human review queue

Usage:
  python orchestrator.py                    # Run now
  python orchestrator.py --dry-run          # Simulate without submitting
  python orchestrator.py --schedule         # Run on daily schedule (7am M-F)
  python orchestrator.py --action era       # Only run ERA upload
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
        ResolutionAction.LAURIS_FAX_VERIFY:      lambda c: handle_lauris_fax_verify(c),
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
    Full daily automation run:
      1. ERA upload
      2. Get denied claims
      3. Route and execute each claim
      4. Log results
    """
    summary = DailyRunSummary()
    human_queue = HumanReviewQueue()
    gap_reporter = GapReporter()
    todays_actions = force_actions or get_todays_primary_actions()

    # -------------------------------------------------------
    # Step 0: Self-learning run counter + report
    # -------------------------------------------------------
    run_count = increment_run_count()
    logger.info("Self-learning run counter", run_count=run_count)

    if should_generate_report():
        logger.info("Generating self-learning report (every 10th run)")
        try:
            report_text = generate_self_learning_report()
            email_report(report_text)
            logger.info("Self-learning report generated and emailed")
        except Exception as e:
            logger.error("Self-learning report failed", error=str(e))
            summary.errors.append(f"Self-learning report failed: {e}")

    logger.info(
        "Starting daily claims automation",
        date=str(date.today()),
        dry_run=DRY_RUN,
        actions=[a.value for a in todays_actions],
    )

    # -------------------------------------------------------
    # Step 0: Fetch Lauris XML AR data (replaces Power BI)
    # -------------------------------------------------------
    ar_claims = []
    try:
        logger.info("Step 0: Fetching Lauris XML outstanding claims")
        ar_claims = fetch_outstanding_claims(lookback_days=365)

        # Enrich with Claim.MD notes and denial reasons
        notes_lookup, denials_lookup = await fetch_claimmd_notes_and_denials()
        enrich_with_claimmd_notes(ar_claims, notes_lookup, denials_lookup)

        total_outstanding = sum(
            c.get("outstanding", 0) for c in ar_claims
        )
        logger.info(
            "Lauris XML AR data loaded",
            ar_claims=len(ar_claims),
            total_outstanding=f"${total_outstanding:,.2f}",
        )

        # Generate unified Excel report
        generate_unified_excel(ar_claims)
    except Exception as e:
        logger.error(
            "Lauris XML fetch failed — will process ALL Claim.MD denials",
            error=str(e),
        )
        summary.errors.append(f"Lauris XML fetch failed: {e}")
        # ar_claims stays empty — all Claim.MD denials will be processed

    # -------------------------------------------------------
    # Step 1: ERA upload (always runs)
    # -------------------------------------------------------
    if ResolutionAction.ERA_UPLOAD in todays_actions:
        logger.info("Step 1: ERA download and staging")
        try:
            era_counts = await download_and_stage_eras()
            summary.eras_uploaded = era_counts.get("staged", 0)
            logger.info("ERA step complete", **era_counts)
        except Exception as e:
            logger.error("ERA step failed — continuing with claim processing", error=str(e))
            summary.errors.append(f"ERA step failed: {e}")

    # Step 1b: Post pending ERAs to Lauris via EDI Results web page
    if ResolutionAction.ERA_UPLOAD in todays_actions:
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
            logger.error("ERA posting failed — continuing", error=str(e))
            summary.errors.append(f"ERA posting failed: {e}")

    # Step 1c: Pre-billing checks (run BEFORE submission to catch issues)
    logger.info("Step 1c: Running pre-billing checks")
    try:
        from actions.billing_web import get_pending_claims
        pending_claims = await get_pending_claims()
        if pending_claims:
            pre_billing_result = run_pre_billing_checks(pending_claims)
            pb_summary = pre_billing_result["summary"]
            logger.info(
                "Pre-billing checks complete",
                checked=pb_summary["total_checked"],
                passed=pb_summary["total_passed"],
                fixed=pb_summary["total_fixed"],
                blocked=pb_summary["total_blocked"],
            )
            if pb_summary["total_blocked"] > 0:
                logger.warning(
                    f"{pb_summary['total_blocked']} claim(s) blocked from billing"
                )
    except ImportError:
        logger.debug("get_pending_claims not available — skipping pre-billing checks")
    except Exception as e:
        logger.error("Pre-billing checks failed — continuing", error=str(e))
        summary.errors.append(f"Pre-billing checks failed: {e}")

    # Step 1d: Billing submission (runs whenever called, not Wednesday-only)
    if True:  # No day restriction — billing runs whenever called
        logger.info("Step 1d: Billing submission")
        try:
            from actions.billing_web import run_billing_submission
            billing_result = await run_billing_submission()
            if billing_result.get("errors"):
                summary.errors.extend(billing_result["errors"])
        except Exception as e:
            logger.error("Billing submission failed", error=str(e))
            summary.errors.append(f"Billing failed: {e}")

    # -------------------------------------------------------
    # Step 2: Get claims from Claim.MD (API preferred, browser fallback)
    # -------------------------------------------------------
    logger.info("Step 2: Fetching denied claims from Claim.MD")
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

    summary.claims_at_start = len(claims)
    logger.info(f"Retrieved {len(claims)} claims to process")

    if not claims:
        await _post_summary(summary, human_queue)
        return summary

    # -------------------------------------------------------
    # Step 2b: Cross-reference Claim.MD denials with Power BI AR data
    # -------------------------------------------------------
    if ar_claims:
        pre_filter_count = len(claims)
        filtered_claims = []
        skipped_not_in_ar = 0

        for claim in claims:
            # Rejected claims (status "R") always get processed —
            # rejections mean the claim never reached the payer
            if claim.status == ClaimStatus.REJECTED:
                filtered_claims.append(claim)
                continue

            # Check if this claim appears in the AR data (still unpaid/underpaid)
            ar_match = is_claim_in_ar(claim.client_id, claim.dos, ar_claims)
            if ar_match:
                # Claim is in AR — still needs work, process it
                filtered_claims.append(claim)
            else:
                # Claim NOT in AR — likely already resolved, skip it
                skipped_not_in_ar += 1
                logger.debug(
                    "Skipping claim — not in Power BI AR (already resolved)",
                    claim_id=claim.claim_id,
                    client=claim.client_name,
                    member=claim.client_id,
                    dos=str(claim.dos),
                )

        claims = filtered_claims
        logger.info(
            "AR cross-reference complete",
            before=pre_filter_count,
            after=len(claims),
            skipped_resolved=skipped_not_in_ar,
        )
    else:
        logger.warning(
            "No AR data available — processing ALL Claim.MD denials without filtering"
        )

    # -------------------------------------------------------
    # Step 3: Route and execute each claim
    # -------------------------------------------------------
    # Rural Rate Reductions first (write off immediately per SOP)
    rrr_claims = [c for c in claims if DenialCode.RURAL_RATE_REDUCTION in c.denial_codes]
    other_claims = [c for c in claims if DenialCode.RURAL_RATE_REDUCTION not in c.denial_codes]

    logger.info(f"Rural Rate Reduction claims: {len(rrr_claims)}, Other: {len(other_claims)}")

    # Process RRR write-offs first
    for claim in rrr_claims:
        result = await handle_write_off(claim)
        summary.write_offs += 1
        summary.claims_completed += 1
        summary.results.append(result)
        _log_to_sheets(claim, result)

    # Process remaining claims (respect today's action filter)
    processed = 0
    for claim in other_claims[:max_claims]:
        action, reason = router.route(claim)

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
                denial_type=claim.denial_codes[0].value if claim.denial_codes else "unknown",
                gap_category=gap_cat,
                dollar_amount=claim.billed_amount,
                resolution=result.action_taken.value,
                status="resolved" if result.success else ("write_off" if result.action_taken == ResolutionAction.WRITE_OFF else "pending"),
            )

        processed += 1

        # Small delay between claims to avoid hammering portals
        await asyncio.sleep(1.5)

    # -------------------------------------------------------
    # Step 3a: Check if previous autonomous corrections resolved
    # -------------------------------------------------------
    try:
        from reporting.autonomous_tracker import check_resolved_corrections
        resolved_result = await check_resolved_corrections()
        if resolved_result["resolved"] > 0:
            logger.info(
                "Autonomous corrections resolved",
                resolved=resolved_result["resolved"],
                dollars=resolved_result["dollars_recovered"],
            )
    except Exception as e:
        logger.warning("Resolved corrections check failed", error=str(e))

    # -------------------------------------------------------
    # Step 3b: Flush consolidated queues
    # -------------------------------------------------------
    try:
        await flush_phone_call_queue()
    except Exception as e:
        logger.warning("Phone call queue flush failed", error=str(e))

    # Weekly write-off approval queue (flush on Fridays)
    if date.today().weekday() == 4:  # Friday
        try:
            await flush_writeoff_approval_queue()
        except Exception as e:
            logger.warning("Write-off queue flush failed", error=str(e))

    # Flush $0 claims and suspected duplicates (weekly to Justin)
    try:
        from sources.claimmd_api import (
            flush_zero_dollar_claims, flush_suspected_duplicates,
        )
        await flush_zero_dollar_claims()
        await flush_suspected_duplicates()
    except Exception as e:
        logger.warning("$0/duplicate flush failed", error=str(e))

    # Check for self-learning approval replies from nm@
    try:
        approval_results = check_self_learning_approvals()
        if approval_results:
            approved = [r for r in approval_results if r.get("action") == "approved"]
            rejected = [r for r in approval_results if r.get("action") == "rejected"]
            logger.info(
                "Self-learning proposals processed via email",
                approved_count=len(approved),
                rejected_count=len(rejected),
                approved_ids=[a["proposal_id"] for a in approved],
                rejected_ids=[r["proposal_id"] for r in rejected],
            )
    except Exception as e:
        logger.warning("Approval reply check failed", error=str(e))

    # -------------------------------------------------------
    # Step 3c: Poll ClickUp for completed tasks with responses
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
        # Check open tasks for new comments (conversational follow-up)
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
    # Step 4: Post summary
    # -------------------------------------------------------
    human_queue.save()
    await _post_summary(summary, human_queue)

    # Post weekly performance report on Fridays
    if date.today().weekday() == 4:  # Friday
        try:
            report = gap_reporter.generate_performance_report_text()
            await clickup.post_comment(report)
            logger.info("Weekly performance report posted to ClickUp")

            # Check training triggers and create ClickUp tasks
            triggers = gap_reporter.get_training_triggers()
            if triggers:
                from actions.clickup_tasks import ClickUpTaskCreator
                task_creator = ClickUpTaskCreator()
                for staff, gap_cat, count in triggers:
                    await task_creator.create_training_flag_task(
                        staff_name=staff,
                        gap_category=gap_cat,
                        count=count,
                    )
                logger.info("Training flag tasks created",
                            count=len(triggers))

            # Check write-off threshold
            if gap_reporter.check_writeoff_threshold():
                await clickup.post_comment(
                    f"ALERT: Weekly write-offs exceed $2,000 threshold. "
                    f"Review required by Nicholas and Desiree. "
                    f"#AUTO #{date.today().strftime('%m/%d/%y')}"
                )
        except Exception as e:
            logger.warning("Failed to post performance report", error=str(e))

    gap_reporter.close()

    logger.info(
        "Daily run complete",
        completed=summary.claims_completed,
        human_flags=summary.human_review_flags,
        errors=len(summary.errors),
    )
    return summary


async def _post_summary(summary: DailyRunSummary, human_queue: HumanReviewQueue):
    """Post daily comment to ClickUp + save human review queue."""
    comment = summary.to_clickup_comment()
    if human_queue.count > 0:
        comment += "\n\n" + human_queue.to_summary_text()
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
        choices=["era", "correct", "recon", "appeal", "writeoff", "auth", "fax", "all", "today"],
        default="all",
        help="Run only a specific action type",
    )
    parser.add_argument("--max-claims", type=int, default=MAX_CLAIMS_PER_RUN)
    parser.add_argument("--full-pull", action="store_true",
                        help="Pull ALL claims from Claim.MD (not just new since last run)")
    return parser.parse_args()


ALL_ACTIONS = list(ResolutionAction)

ACTION_MAP = {
    "era":      [ResolutionAction.ERA_UPLOAD],
    "correct":  [ResolutionAction.CORRECT_AND_RESUBMIT],
    "recon":    [ResolutionAction.RECONSIDERATION],
    "appeal":   [ResolutionAction.APPEAL_STEP3],
    "writeoff": [ResolutionAction.WRITE_OFF],
    "auth":     [ResolutionAction.MCO_PORTAL_AUTH_CHECK],
    "fax":      [ResolutionAction.LAURIS_FAX_VERIFY],
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
