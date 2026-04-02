"""
actions/era_poster.py
---------------------
Automated ERA posting via Lauris EDI Results web page.

ERA/835 files are automatically received by Lauris as EDI files
(visible in AR Reports > EDI Items). This module:
  1. Lists unposted EDI files
  2. Classifies each (skip irregular: Anthem Mary's, etc.)
  3. Posts standard ERAs by selecting from dropdown + clicking "Post Selected File"
  4. Logs results

The EDI Results page is at: /ar/ClosedBillingEDIResults.aspx
Files are named: era_{payerid}_{eraid}.x12
"""
from __future__ import annotations

import asyncio
import json
import re
from datetime import date
from pathlib import Path
from typing import List, Tuple

from config.settings import DRY_RUN
from lauris.billing import LaurisSession
from logging_utils.logger import get_logger, ClickUpLogger

logger = get_logger("era_poster")
clickup = ClickUpLogger()

# Track which ERA files have been posted to Lauris (persistent)
POSTED_ERAS_FILE = Path("data/posted_eras.json")

# PLB adjustments (ACH fees, etc.) at or below this amount are auto-written-off.
# Anything above this threshold is flagged for human review.
PLB_AUTO_WRITEOFF_MAX = 25.00

# Directory where downloaded 835 files are stored
ERA_DOWNLOAD_DIR = Path("/tmp/claims_work/eras")


def _load_posted_eras() -> set:
    """Load the set of ERA file values already posted to Lauris."""
    if POSTED_ERAS_FILE.exists():
        try:
            with open(POSTED_ERAS_FILE) as f:
                return set(json.load(f))
        except Exception:
            pass
    return set()


def _save_posted_eras(posted: set):
    """Save the set of posted ERA file values."""
    POSTED_ERAS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(POSTED_ERAS_FILE, "w") as f:
        json.dump(sorted(posted), f)


def _parse_plb_adjustments(era_id: str) -> list[dict]:
    """
    Parse PLB (Provider Level Balance) segments from a downloaded 835 file.
    Returns a list of dicts: {check_number, amount, reason_code, npi}.

    PLB format: PLB*npi*date*reason_code:reference*amount
    Common in ERAs where the MCO deducts an ACH fee from the payment.
    """
    # Try to find the 835 file — match by era_id suffix in filename
    matches = list(ERA_DOWNLOAD_DIR.glob(f"era_{era_id}.835"))
    if not matches:
        # Also try matching from the EDI filename (era_{payerid}_{eraid}.x12)
        matches = list(ERA_DOWNLOAD_DIR.glob(f"*{era_id}*.835"))
    if not matches:
        return []

    try:
        content = matches[0].read_text()
    except Exception:
        return []

    adjustments = []
    # 835 segments are separated by ~ (tilde)
    segments = re.split(r"~", content)
    for seg in segments:
        seg = seg.strip()
        if not seg.startswith("PLB"):
            continue
        fields = seg.split("*")
        # PLB*npi*fiscal_year_end*reason:reference*amount
        if len(fields) < 5:
            continue
        npi = fields[1]
        reason_ref = fields[3]  # e.g. "AH:1237881412"
        amount_str = fields[4]
        reason_code = reason_ref.split(":")[0] if ":" in reason_ref else reason_ref
        check_ref = reason_ref.split(":")[1] if ":" in reason_ref else ""
        try:
            amount = abs(float(amount_str))
        except ValueError:
            continue
        adjustments.append({
            "npi": npi,
            "reason_code": reason_code,
            "check_number": check_ref,
            "amount": amount,
        })

    return adjustments


def _extract_era_id_from_filename(file_name: str) -> str:
    """Extract the Claim.MD ERA ID from an EDI filename like 'era_MCC02_72325594.x12 - 04/01/2026'."""
    # Pattern: era_{payerid}_{eraid}.x12
    match = re.search(r"era_[A-Za-z0-9]+_(\d+)", file_name)
    if match:
        return match.group(1)
    # Fallback: era_{eraid}.x12
    match = re.search(r"era_(\d+)", file_name)
    return match.group(1) if match else ""


# Irregular ERA patterns — skip these
IRREGULAR_PATTERNS = [
    (re.compile(r"anthem.*mary|mary.*anthem", re.I), "anthem_marys"),
    (re.compile(r"united.*mary|mary.*united", re.I), "united_marys"),
    (re.compile(r"recoup", re.I), "recoupment"),
    (re.compile(r"straight.*medicaid.*mary|medicaid.*mary.*straight", re.I), "straight_medicaid_marys"),
]

# Post ALL unposted ERAs, but don't go back more than 1 year
MAX_ERA_AGE_DAYS = 365

EDI_RESULTS_PATH = "ar/ClosedBillingEDIResults.aspx"


async def _write_off_plb_adjustment(
    lauris: LaurisSession, base_url: str, adj: dict, reason: str
) -> None:
    """
    Write off a PLB adjustment amount in Lauris Billing Center.
    Navigates to the adjustment/write-off section and applies the PLB fee.
    """
    logger.info(
        "Writing off PLB adjustment",
        amount=adj["amount"],
        check=adj["check_number"],
        npi=adj["npi"],
    )
    await lauris.page.goto(
        f"{base_url}/reports/BillingDash.aspx",
        wait_until="domcontentloaded",
        timeout=20000,
    )
    await asyncio.sleep(2)

    # Look for adjustment/write-off entry point in billing center
    for sel in [
        "a:has-text('Adjustment')", "a:has-text('Write Off')",
        "a:has-text('PLB')", "a[href*='adjust']",
        "a[href*='writeoff']", "a[href*='WriteOff']",
    ]:
        link = await lauris.page.query_selector(sel)
        if link and await link.is_visible():
            await link.click()
            await asyncio.sleep(2)
            break

    # Fill adjustment amount
    for sel in [
        "input[name*='Amount']", "input[name*='amount']",
        "input[id*='Amount']", "input[id*='amount']",
        "input[name*='adj']", "input[type='number']",
    ]:
        field = await lauris.page.query_selector(sel)
        if field:
            await field.fill(f"{adj['amount']:.2f}")
            break

    # Fill reason
    for sel in [
        "textarea[name*='reason']", "textarea[name*='Reason']",
        "input[name*='reason']", "input[name*='Reason']",
        "select[name*='reason']", "textarea[name*='note']",
    ]:
        reason_field = await lauris.page.query_selector(sel)
        if reason_field:
            tag = (await reason_field.get_attribute("tagName") or "").lower()
            if tag == "select":
                await lauris.page.select_option(sel, label="ACH Fee")
            else:
                await reason_field.fill(reason)
            break

    # Fill check/reference number if there's a field for it
    for sel in [
        "input[name*='check']", "input[name*='Check']",
        "input[name*='reference']", "input[name*='Reference']",
    ]:
        ref_field = await lauris.page.query_selector(sel)
        if ref_field:
            await ref_field.fill(adj["check_number"])
            break

    # Submit
    for sel in [
        "button:has-text('Save')", "button:has-text('Submit')",
        "input[value*='Save']", "input[value*='Submit']",
        "button:has-text('Confirm')", "input[value*='Confirm']",
    ]:
        btn = await lauris.page.query_selector(sel)
        if btn and await btn.is_visible():
            await btn.click()
            await asyncio.sleep(2)
            break

    logger.info(
        "PLB adjustment write-off submitted",
        amount=adj["amount"],
        check=adj["check_number"],
    )


async def post_pending_eras() -> dict:
    """
    Post all pending ERA/EDI files in Lauris via the web interface.
    After posting, checks for PLB adjustments (ACH fees) and auto-writes-off
    amounts <= $25. Flags larger PLB amounts for human review.

    Returns dict with counts: posted, skipped_irregular, already_posted, errors.
    """
    result = {
        "posted": 0,
        "skipped_irregular": 0,
        "skipped_old": 0,
        "skipped_already_posted": 0,
        "errors": 0,
        "plb_writeoffs": 0,
        "plb_flagged": 0,
        "posted_files": [],
        "irregular_files": [],
        "plb_details": [],
    }

    if DRY_RUN:
        logger.info("DRY_RUN: Would post pending ERAs")
        return result

    try:
        async with LaurisSession() as lauris:
            base = lauris.login_url.rsplit("/", 1)[0]

            # Navigate to EDI Results page
            await lauris.page.goto(
                f"{base}/{EDI_RESULTS_PATH}",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            await asyncio.sleep(3)

            # Get all EDI file options from dropdown
            options = await lauris.page.query_selector_all(
                "select[name='ddlEDIFiles'] option"
            )
            logger.info(f"Found {len(options)} EDI files in Lauris")

            # Load locally tracked posted ERAs
            already_posted_local = _load_posted_eras()
            logger.info(
                "Loaded posted ERA tracking",
                tracked=len(already_posted_local),
            )

            files_to_post = []
            for opt in options:
                val = await opt.get_attribute("value") or ""
                text = (await opt.inner_text()).strip()

                if val == "0" or not val:
                    continue  # Skip "Choose an EDI File"

                # Check if irregular
                is_irregular = False
                for pattern, irr_type in IRREGULAR_PATTERNS:
                    if pattern.search(text):
                        is_irregular = True
                        result["skipped_irregular"] += 1
                        result["irregular_files"].append(
                            f"{text} ({irr_type})"
                        )
                        logger.info("Skipping irregular ERA",
                                    file=text, type=irr_type)
                        break

                if is_irregular:
                    continue

                # Check if already posted (local tracking)
                if val in already_posted_local:
                    result["skipped_already_posted"] += 1
                    continue

                # Check age — only post recent files
                date_match = re.search(r"(\d{2}/\d{2}/\d{4})", text)
                if date_match:
                    from datetime import datetime, timedelta
                    try:
                        file_date = datetime.strptime(
                            date_match.group(1), "%m/%d/%Y"
                        ).date()
                        if (date.today() - file_date).days > MAX_ERA_AGE_DAYS:
                            result["skipped_old"] += 1
                            continue
                    except ValueError:
                        pass

                files_to_post.append((val, text))

            logger.info(f"Posting {len(files_to_post)} ERA files")

            # Post each file
            for file_val, file_name in files_to_post:
                try:
                    # Select the file from dropdown
                    await lauris.page.select_option(
                        "select[name='ddlEDIFiles']", value=file_val
                    )
                    await asyncio.sleep(0.5)

                    # Click "Post Selected File"
                    await lauris.page.click(
                        "input[name='btnPostFile']", timeout=10000
                    )
                    await asyncio.sleep(3)

                    # Verify success/error after posting
                    page_text = await lauris.page.inner_text("body")
                    posting_error = False
                    for error_indicator in [
                        "error", "failed", "unable to post",
                        "exception", "could not process",
                    ]:
                        if error_indicator in page_text.lower():
                            posting_error = True
                            break

                    if posting_error:
                        # Take screenshot for debugging
                        try:
                            screenshot_path = f"/tmp/claims_work/era_post_error_{file_val}.png"
                            await lauris.page.screenshot(path=screenshot_path)
                            logger.error(
                                "ERA posting error detected — screenshot saved",
                                file=file_name,
                                screenshot=screenshot_path,
                            )
                        except Exception:
                            logger.error("ERA posting error detected",
                                         file=file_name)
                        result["errors"] += 1
                        continue

                    result["posted"] += 1
                    result["posted_files"].append(file_name)
                    already_posted_local.add(file_val)
                    logger.info("ERA posted successfully", file=file_name)

                    # Check for PLB adjustments (ACH fees, etc.)
                    era_id = _extract_era_id_from_filename(file_name)
                    if era_id:
                        plb_adjustments = _parse_plb_adjustments(era_id)
                        for adj in plb_adjustments:
                            if adj["amount"] <= PLB_AUTO_WRITEOFF_MAX:
                                # Auto-write-off small PLB fees
                                reason = (
                                    f"PLB adjustment — ACH fee "
                                    f"(check {adj['check_number']}, "
                                    f"${adj['amount']:.2f})"
                                )
                                try:
                                    await _write_off_plb_adjustment(
                                        lauris, base, adj, reason
                                    )
                                    result["plb_writeoffs"] += 1
                                    result["plb_details"].append(
                                        f"{file_name}: wrote off "
                                        f"${adj['amount']:.2f} PLB fee"
                                    )
                                    logger.info(
                                        "PLB fee auto-written-off",
                                        era_id=era_id,
                                        amount=adj["amount"],
                                        check=adj["check_number"],
                                    )
                                except Exception as plb_err:
                                    logger.error(
                                        "PLB write-off failed",
                                        era_id=era_id,
                                        amount=adj["amount"],
                                        error=str(plb_err),
                                    )
                                    result["plb_flagged"] += 1
                                    result["plb_details"].append(
                                        f"{file_name}: FAILED write-off "
                                        f"${adj['amount']:.2f} — "
                                        f"needs manual review"
                                    )
                            else:
                                # Flag large PLB for human review
                                result["plb_flagged"] += 1
                                result["plb_details"].append(
                                    f"{file_name}: PLB ${adj['amount']:.2f} "
                                    f"exceeds ${PLB_AUTO_WRITEOFF_MAX:.0f} "
                                    f"— needs manual review"
                                )
                                logger.warning(
                                    "PLB adjustment exceeds auto-writeoff "
                                    "threshold — flagged for human review",
                                    era_id=era_id,
                                    amount=adj["amount"],
                                    threshold=PLB_AUTO_WRITEOFF_MAX,
                                )

                except Exception as e:
                    result["errors"] += 1
                    logger.error("ERA post failed",
                                 file=file_name, error=str(e))

            # Save posted ERA tracking
            _save_posted_eras(already_posted_local)
            logger.info(
                "Posted ERA tracking saved",
                total_tracked=len(already_posted_local),
                newly_posted=result["posted"],
            )

            # Post ClickUp summary
            if result["posted"] > 0:
                posted_list = "\n".join(
                    f"  - {f}" for f in result["posted_files"]
                )
                irregular_note = ""
                if result["irregular_files"]:
                    irregular_list = "\n".join(
                        f"  - {f}" for f in result["irregular_files"]
                    )
                    irregular_note = (
                        f"\n\nIrregular ERAs (manual handling):\n"
                        f"{irregular_list}"
                    )

                plb_note = ""
                if result["plb_details"]:
                    plb_list = "\n".join(
                        f"  - {d}" for d in result["plb_details"]
                    )
                    plb_note = (
                        f"\n\nPLB Adjustments (ACH fees):\n{plb_list}"
                    )

                await clickup.post_comment(
                    f"ERA Posting Complete — {result['posted']} ERA(s) "
                    f"posted to Lauris via EDI Results.\n\n"
                    f"Posted:\n{posted_list}"
                    f"{irregular_note}"
                    f"{plb_note}\n\n"
                    f"#AUTO #{date.today().strftime('%m/%d/%y')}"
                )

    except Exception as e:
        logger.error("ERA posting failed", error=str(e))
        result["errors"] += 1

    logger.info("ERA posting complete", **{
        k: v for k, v in result.items()
        if k not in ("posted_files", "irregular_files", "plb_details")
    })
    return result
