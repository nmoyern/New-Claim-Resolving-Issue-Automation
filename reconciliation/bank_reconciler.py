"""
reconciliation/bank_reconciler.py
----------------------------------
Claim Bank Reconciliation Report

Reconciles MCO payments (from ERAs) against actual bank deposits:
  - KJLN → Wells Fargo
  - Mary's Home → Southern Bank
  - NHCS → Bank of America

Simplified workflow (March 2026):
  1. Get new ERA payments from Claim.MD API
  2. Look at incoming funds and determine the payor
  3. Track payments with unique codes
  4. Escalate unreconciled payments after 7 business days
  5. Check email for manual confirmations (code_PAID_DATE)
  6. Generate the Claim Bank Reconciliation Report

No complex bank transaction matching — just track incoming funds
and which payor they came from.
"""
from __future__ import annotations

import asyncio
import os
from datetime import date, datetime
from typing import List, Optional

from config.entities import get_entity_by_npi
from config.settings import DRY_RUN
from sources.claimmd_api import ClaimMDAPI
from reconciliation.payment_tracker import PaymentTracker
from reconciliation.email_monitor import EmailMonitor
from logging_utils.logger import get_logger, ClickUpLogger

logger = get_logger("bank_reconciler")
clickup = ClickUpLogger()


async def run_bank_reconciliation() -> dict:
    """
    Run the full bank reconciliation process.
    Returns dict with results summary.
    """
    result = {
        "new_payments_tracked": 0,
        "bank_verified": 0,
        "email_commands_processed": 0,
        "escalated": 0,
        "report": "",
    }

    tracker = PaymentTracker()

    # Step 1: Get new ERA payments from Claim.MD
    api = ClaimMDAPI()
    if api.key:
        try:
            era_list = await api.get_era_list(received_date="today")
            for era in era_list:
                era_id = str(era.get("eraid", ""))
                if not era_id:
                    continue

                code = tracker.add_payment(
                    era_id=era_id,
                    check_number=era.get("check_number", ""),
                    check_type=era.get("check_type", "eft"),
                    payer_name=era.get("payer_name", ""),
                    payer_id=era.get("payerid", ""),
                    provider_npi=era.get("prov_npi", ""),
                    provider_name=era.get("claimmd_prov_name", ""),
                    program=_npi_to_program(era.get("prov_npi", "")),
                    paid_amount=float(era.get("paid_amount", "0") or "0"),
                    paid_date=era.get("paid_date", ""),
                    received_date=era.get("received_time", ""),
                )
                result["new_payments_tracked"] += 1

            logger.info("ERA payments tracked",
                        count=result["new_payments_tracked"])
        except Exception as e:
            logger.error("Failed to get ERA payments", error=str(e))

    # Step 2: Check email for manual payment confirmations
    try:
        monitor = EmailMonitor()
        commands = monitor.check_for_commands()

        for cmd in commands:
            if cmd["type"] == "paid":
                success = tracker.mark_paid(
                    cmd["unique_code"],
                    paid_date=cmd.get("paid_date", ""),
                    marked_by=cmd["sender"],
                )
                if success:
                    result["email_commands_processed"] += 1
                    logger.info("Payment marked paid via email",
                                code=cmd["unique_code"],
                                sender=cmd["sender"])

            elif cmd["type"] == "writeoff":
                # Process Excel write-off list
                for attachment in cmd.get("attachments", []):
                    await _process_writeoff_excel(
                        attachment["path"], tracker
                    )
                result["email_commands_processed"] += 1

            elif cmd["type"] == "cancel":
                logger.warning("CANCEL command received",
                               sender=cmd["sender"],
                               subject=cmd["subject"])
                # Handle cancellation — reverse last action for this subject
                # This is logged for human review

        monitor.disconnect()
    except Exception as e:
        logger.warning("Email monitoring failed", error=str(e))

    # Step 3: Auto-verify pending payments against bank deposits
    try:
        from reconciliation.bank_portals import get_bank_portal
        pending = tracker.get_pending_payments()
        # Group by program to minimize portal logins
        by_program = {}
        for p in pending:
            prog = p.get("program", "UNKNOWN")
            by_program.setdefault(prog, []).append(p)

        for program, payments in by_program.items():
            portal = get_bank_portal(program, headless=True)
            if not portal:
                continue
            try:
                async with portal:
                    deposits = await portal.get_recent_deposits(days=14)
                    for payment in payments:
                        for dep in deposits:
                            if abs(dep["amount"] - payment["paid_amount"]) < 0.01:
                                if portal._dates_close(
                                    dep.get("date", ""),
                                    payment.get("paid_date", ""),
                                    max_days=5,
                                ):
                                    tracker.mark_paid(
                                        payment["unique_code"],
                                        paid_date=dep.get("date", ""),
                                        marked_by="bank_auto_verify",
                                    )
                                    result["bank_verified"] += 1
                                    logger.info(
                                        "Payment auto-verified via bank",
                                        code=payment["unique_code"],
                                        amount=payment["paid_amount"],
                                        bank=portal.BANK_NAME,
                                    )
                                    break
            except Exception as e:
                logger.warning(
                    "Bank portal check failed",
                    bank=program, error=str(e),
                )
    except Exception as e:
        logger.warning("Bank auto-verification skipped", error=str(e))

    # Step 4: Check for payments needing escalation (7+ business days)
    overdue = tracker.get_escalation_needed(business_days=7)
    for payment in overdue:
        if not DRY_RUN:
            # Create ClickUp task for Justin
            try:
                from actions.clickup_tasks import (
                    ClickUpTaskCreator, get_assignees, _next_business_day,
                )
                from actions.clickup_poller import store_task_metadata
                tc = ClickUpTaskCreator()
                task_id = await tc.create_task(
                    list_id=os.getenv("CLICKUP_LIST_ID", ""),
                    name=(
                        f"Payment Not Received: {payment['payer_name']} "
                        f"${payment['paid_amount']:,.2f} "
                        f"(Code: {payment['unique_code']})"
                    ),
                    description=(
                        f"MCO says they paid but funds not verified in bank.\n\n"
                        f"Tracking Code: {payment['unique_code']}\n"
                        f"Payer: {payment['payer_name']}\n"
                        f"Amount: ${payment['paid_amount']:,.2f}\n"
                        f"Check/EFT: {payment['check_number']} ({payment['check_type']})\n"
                        f"Paid Date: {payment['paid_date']}\n"
                        f"Bank: {payment['bank_entity']}\n"
                        f"Provider: {payment['provider_name']}\n\n"
                        f"This payment has been unreconciled for 7+ business days.\n"
                        f"Please investigate and verify with the bank.\n\n"
                        f"Once verified, mark this task complete and comment "
                        f"with the date received.\n\n"
                        f"#AUTO #{date.today().strftime('%m/%d/%y')}"
                    ),
                    assignees=get_assignees("bank_verify"),
                    due_date=_next_business_day(),
                    priority=2,  # High
                )
                if task_id:
                    store_task_metadata(
                        task_id, "bank_verify",
                        unique_code=payment["unique_code"],
                    )
                tracker.mark_escalated(payment["unique_code"], task_id or "")
                result["escalated"] += 1
            except Exception as e:
                logger.error("Escalation failed", code=payment["unique_code"],
                             error=str(e))

    # Step 5: Generate report
    report = tracker.generate_reconciliation_report()
    result["report"] = report

    # Post report to ClickUp
    if not DRY_RUN:
        await clickup.post_comment(report)

    tracker.close()
    logger.info("Bank reconciliation complete", **{
        k: v for k, v in result.items() if k != "report"
    })
    return result


async def _process_writeoff_excel(file_path: str, tracker: PaymentTracker):
    """Process an Excel file containing PCN write-off list."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(file_path)
        ws = wb.active

        for row in ws.iter_rows(min_row=2, values_only=True):
            if len(row) >= 2:
                pcn = str(row[0]).strip() if row[0] else ""
                reason = str(row[1]).strip() if row[1] else "Underpayment write-off"
                amount = float(row[2]) if len(row) > 2 and row[2] else 0

                if pcn:
                    tracker.mark_write_off(pcn, amount, reason)
                    logger.info("Write-off processed from email",
                                pcn=pcn, reason=reason[:40])
    except ImportError:
        # Try with csv if openpyxl not available
        import csv
        with open(file_path) as f:
            reader = csv.reader(f)
            next(reader, None)  # skip header
            for row in reader:
                if len(row) >= 2:
                    pcn = row[0].strip()
                    reason = row[1].strip() if len(row) > 1 else "Write-off"
                    amount = float(row[2]) if len(row) > 2 else 0
                    if pcn:
                        tracker.mark_write_off(pcn, amount, reason)
    except Exception as e:
        logger.error("Write-off Excel processing failed",
                     file=file_path, error=str(e))


def _npi_to_program(npi: str) -> str:
    entity = get_entity_by_npi(npi)
    return entity.key if entity else "UNKNOWN"
