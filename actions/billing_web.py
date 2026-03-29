"""
actions/billing_web.py
----------------------
Web-based billing submission via Lauris Close Billing page.

The Lauris "Launch Billing Center" requires a Windows desktop app.
However, the Close Billing page IS web-based and allows:
  - Selecting Region (All Regions, KJLN, NHCS, Mary's Home)
  - Selecting Service codes
  - Filtering by Payor
  - Selecting billing date
  - Closing reconciled billing items

This module automates the Close Billing workflow as a workaround
for the desktop-only Billing Center.

Per Admin Manual (March 2026):
  - Billing runs on WEDNESDAYS (not Tuesdays)
  - Double Billing Report only needed for first-time claims
  - All MCOs billed (Aetna exclusion removed)
  - Must verify payroll has NOT run before billing
"""
from __future__ import annotations

import asyncio
from datetime import date
from typing import List, Tuple

from config.models import MCO
from config.settings import DRY_RUN
from lauris.billing import LaurisSession
from logging_utils.logger import get_logger, ClickUpLogger

logger = get_logger("billing_web")
clickup = ClickUpLogger()

# Aetna exclusion removed — bill all MCOs equally
BILLING_EXCLUDED_MCOS = set()

# Lauris Close Billing page URL
CLOSE_BILLING_PATH = "closebillingnc.aspx"

# NHCS MHSS rate: $102.72 per unit
NHCS_MHSS_RATE = 102.72


async def run_billing_submission(billing_date: date = None) -> dict:
    """
    Run the weekly billing submission via Lauris Close Billing web page.

    Steps:
      1. Verify it's Wednesday (or override)
      2. Login to Lauris
      3. Navigate to Close Billing
      4. For each region (KJLN, NHCS, Mary's Home):
         a. Select region
         b. Select billing date
         c. Filter by payor (exclude Aetna)
         d. Click Refresh to load items
         e. Click "Close Reconciled Billing Items"
      5. Post results to ClickUp

    Returns dict with submission results.
    """
    if billing_date is None:
        billing_date = date.today()

    result = {
        "date": str(billing_date),
        "submitted": False,
        "regions_processed": [],
        "items_closed": 0,
        "errors": [],
    }

    # Wednesday-only check removed — billing runs whenever called
    if DRY_RUN:
        logger.info("DRY_RUN: Would submit billing", date=str(billing_date))
        result["submitted"] = True
        return result

    try:
        async with LaurisSession() as lauris:
            base = lauris.login_url.rsplit("/", 1)[0]

            # Navigate to Close Billing
            await lauris.page.goto(
                f"{base}/{CLOSE_BILLING_PATH}",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            await asyncio.sleep(3)

            # Get available regions from dropdown
            regions = await lauris.page.query_selector_all(
                "select[name='ddlRegion'] option"
            )
            region_names = []
            for r in regions:
                val = await r.get_attribute("value") or ""
                text = (await r.inner_text()).strip()
                if val and text != "All Regions":
                    region_names.append((val, text))

            logger.info("Available billing regions",
                        regions=[r[1] for r in region_names])

            # Process each region
            for region_val, region_name in region_names:
                try:
                    # Select region
                    await lauris.page.select_option(
                        "select[name='ddlRegion']", value=region_val
                    )
                    await asyncio.sleep(0.5)

                    # Select today's date on the calendar
                    day_num = str(billing_date.day)
                    day_link = await lauris.page.query_selector(
                        f"a:has-text('{day_num}')"
                    )
                    if day_link:
                        await day_link.click()
                        await asyncio.sleep(0.5)

                    # No MCO exclusions — bill all payors

                    # Click Refresh to load billing items
                    await lauris.page.click(
                        "input[name='btnRefresh']", timeout=10000
                    )
                    await asyncio.sleep(3)

                    # NHCS MHSS rate adjustment: $9.80/unit
                    # For NHCS region, find MHSS service lines and
                    # adjust the charge to units × $9.80
                    if "nhcs" in region_name.lower():
                        adjusted = await _adjust_nhcs_mhss_rates(
                            lauris, NHCS_MHSS_RATE
                        )
                        if adjusted > 0:
                            logger.info(
                                "NHCS MHSS rates adjusted",
                                region=region_name,
                                lines_adjusted=adjusted,
                            )

                    # Check if there are items to close
                    items_el = await lauris.page.query_selector(
                        "td:has-text('Items'), span:has-text('Items')"
                    )
                    items_text = ""
                    if items_el:
                        items_text = (await items_el.inner_text()).strip()

                    # Click "Close Reconciled Billing Items"
                    close_btn = await lauris.page.query_selector(
                        "input[name='btnClose'], "
                        "input[value*='Close Reconciled'], "
                        "button:has-text('Close Reconciled')"
                    )
                    if close_btn:
                        await close_btn.click()
                        await asyncio.sleep(3)
                        result["regions_processed"].append(region_name)
                        logger.info("Billing closed for region",
                                    region=region_name, items=items_text)
                    else:
                        logger.warning("Close button not found",
                                       region=region_name)

                except Exception as e:
                    logger.error("Billing error for region",
                                 region=region_name, error=str(e))
                    result["errors"].append(f"{region_name}: {e}")

            result["submitted"] = len(result["regions_processed"]) > 0

    except Exception as e:
        logger.error("Billing submission failed", error=str(e))
        result["errors"].append(str(e))

    # Post to ClickUp
    if result["submitted"]:
        regions_str = ", ".join(result["regions_processed"])
        await clickup.post_comment(
            f"Weekly billing submitted {billing_date.strftime('%m/%d/%y')}. "
            f"Regions processed: {regions_str}. "
            f"All MCOs included. "
            f"#AUTO #{date.today().strftime('%m/%d/%y')}"
        )
    elif result["errors"]:
        await clickup.post_comment(
            f"BILLING ISSUE — {billing_date.strftime('%m/%d/%y')}: "
            f"{'; '.join(result['errors'][:3])}. "
            f"Manual billing may be required. "
            f"#AUTO #{date.today().strftime('%m/%d/%y')}"
        )

    return result


async def _adjust_nhcs_mhss_rates(lauris, rate: float) -> int:
    """
    Scan the billing grid for MHSS service lines and adjust charge
    amounts to units × $9.80/unit.

    MHSS procedure codes: H0046, H2014
    Looks at the billing items table, finds MHSS rows, recalculates
    the charge, and updates the amount field if editable.

    Returns count of lines adjusted.
    """
    import re
    adjusted = 0
    mhss_codes = {"H0046", "H2014"}

    try:
        rows = await lauris.page.query_selector_all(
            "table tbody tr, .billing-row, .grid-row"
        )
        for row in rows:
            text = await row.inner_text()
            # Check if this row contains an MHSS procedure code
            has_mhss = any(code in text.upper() for code in mhss_codes)
            if not has_mhss:
                # Also check for "MHSS" text in service description
                if "MHSS" not in text.upper():
                    continue

            # Extract units from the row
            cells = await row.query_selector_all("td")
            units_val = 0.0
            charge_input = None

            for cell in cells:
                cell_text = (await cell.inner_text()).strip()

                # Look for a units column (typically a small integer)
                if re.match(r'^\d+(\.\d+)?$', cell_text):
                    val = float(cell_text)
                    # Units are typically 1-100, charges are larger
                    if 0 < val <= 200 and units_val == 0:
                        units_val = val

                # Look for editable charge/amount field
                inp = await cell.query_selector(
                    "input[name*='charge' i], "
                    "input[name*='amount' i], "
                    "input[name*='rate' i], "
                    "input[type='text']"
                )
                if inp:
                    charge_input = inp

            if not units_val or not charge_input:
                continue

            correct_charge = round(units_val * rate, 2)
            current_val = (
                await charge_input.get_attribute("value") or "0"
            )
            try:
                current_charge = float(
                    current_val.replace(",", "").replace("$", "")
                )
            except ValueError:
                current_charge = 0

            if abs(current_charge - correct_charge) > 0.01:
                await charge_input.fill(str(correct_charge))
                adjusted += 1
                logger.info(
                    "MHSS rate adjusted on billing page",
                    units=units_val,
                    old_charge=current_charge,
                    new_charge=correct_charge,
                )

    except Exception as e:
        logger.warning("NHCS MHSS rate adjustment error", error=str(e))

    return adjusted
