"""
actions/billing_submission.py
Weekly billing submission: Double Billing Report -> Submit (non-Aetna) -> Post to ClickUp
Per Admin Manual: runs once/week PRIOR to payroll, WEDNESDAY standard.

Note (March 2026 correction): Double Billing Report only needed for FIRST-TIME claims.
Claims already in PowerBI/Claim.MD have already been billed, so no double billing check needed.
"""
from __future__ import annotations
import asyncio
import re
from datetime import date
from typing import List, Tuple

from config.models import MCO
from config.settings import DRY_RUN
from lauris.billing import LaurisSession
from logging_utils.logger import get_logger, ClickUpLogger

logger = get_logger("billing_submission")
clickup = ClickUpLogger()
# Aetna exclusion removed — bill Aetna like all other MCOs
BILLING_EXCLUDED_MCOS = set()


class BillingSubmitter:
    # verify_payroll_not_run() removed — payroll is a separate automation
    # run_double_billing_report() removed — double billing is a separate automation

    async def submit_billing(self, billing_date: date) -> Tuple[int, int]:
        if DRY_RUN:
            logger.info("DRY_RUN: Would submit billing", date=str(billing_date))
            return 0, 0
        submitted = errors = 0
        try:
            async with LaurisSession() as lauris:
                await lauris._navigate_to_billing_center()
                await lauris.safe_fill(
                    "input[name*='billing_date'], input[type='date']",
                    billing_date.strftime("%m/%d/%Y"))
                await asyncio.sleep(0.5)
                # Uncheck excluded MCOs (Aetna)
                for cb in await lauris.page.query_selector_all("input[type='checkbox'][name*='mco']"):
                    lbl = await lauris.page.query_selector(f"label[for='{await cb.get_attribute('id')}']")
                    lbl_text = (await lbl.inner_text()).upper() if lbl else ""
                    if any(m.value.upper() in lbl_text for m in BILLING_EXCLUDED_MCOS):
                        if await cb.is_checked():
                            await cb.uncheck()
                        logger.info("Excluded from billing", mco=lbl_text)
                await lauris.safe_click("button:has-text('Submit Billing'), button:has-text('Submit')")
                await asyncio.sleep(3)
                r = await lauris.page.query_selector(".result, .alert, .submission-result")
                if r:
                    t = await r.inner_text()
                    m = re.search(r"(\d+)\s+claim", t, re.I)
                    submitted = int(m.group(1)) if m else 1
        except Exception as e:
            logger.error("Billing submission failed", error=str(e))
            errors = 1
        return submitted, errors


async def run_weekly_billing() -> dict:
    result = {"date": str(date.today()), "skipped": False, "submitted": 0, "errors": 0}
    sub = BillingSubmitter()

    # Payroll check and double billing report removed — separate automations

    submitted, errors = await sub.submit_billing(date.today())
    result.update({"submitted": submitted, "errors": errors})
    await clickup.post_comment(
        f"Weekly billing submitted {date.today().strftime('%m/%d/%y')}. "
        f"{submitted} claims billed (all MCOs). "
        "#AUTO #" + date.today().strftime("%m/%d/%y"))
    return result


def get_billing_period():
    """
    Return (start, end) dates for the current billing week.
    Billing period: Monday through Sunday of the most recent full week.
    (Billing runs Tuesday before payroll; covers the prior Monday–Sunday.)
    """
    from datetime import timedelta
    today = date.today()
    # This week's Monday
    monday = today - timedelta(days=today.weekday())
    # Prior Monday (last complete week)
    last_monday = monday - timedelta(weeks=1)
    last_sunday = last_monday + timedelta(days=6)
    return last_monday, last_sunday
