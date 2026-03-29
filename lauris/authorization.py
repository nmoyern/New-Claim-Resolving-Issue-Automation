"""
lauris/authorization.py
-----------------------
Check Lauris Authorization Management page (authmanage.aspx) for auth records.

This is Step 1 in the auth verification cascade — check Lauris grid data
before hitting MCO portals.

Grid columns (14 cells per row, confirmed from live testing):
  Start Date, End Date, Vendor, Payor, Authorization #, Record No, Services,
  plus action icons (edit, contact log, scanned docs, upload, delete/approve/reject).

The scanned document (.iif) is Lauris's proprietary format and CANNOT be read
programmatically. We note whether it exists and cross-reference grid data with
MCO portal data. Discrepancies are flagged for human review.
"""
from __future__ import annotations

import asyncio
import re
from datetime import date, datetime, timedelta
from typing import List, Optional

from playwright.async_api import Page

from lauris.billing import LaurisSession
from lauris.diagnosis import _lookup_uid_from_record_number
from config.settings import DRY_RUN
from logging_utils.logger import get_logger

logger = get_logger("lauris_authorization")

# ---------------------------------------------------------------------------
# Date parsing helper
# ---------------------------------------------------------------------------

def _parse_date(date_str: str) -> Optional[date]:
    """Parse dates in common Lauris grid formats."""
    for fmt in ("%m/%d/%Y", "%m/%d/%y", "%Y-%m-%d"):
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# Grid parsing
# ---------------------------------------------------------------------------

async def _parse_auth_grid(page: Page) -> List[dict]:
    """
    Parse the authorization grid (#ctl00_ContentPlaceHolder1_gridAuth)
    and return a list of auth record dicts.

    Each row has 14 cells. Key data columns:
      - Cell 0: Start Date
      - Cell 1: End Date
      - Cell 2: Vendor
      - Cell 3: Payor
      - Cell 4: Authorization #
      - Cell 5: Record No
      - Cell 6: Services (e.g. "H0046 (78.00)")

    The presence of a viewPerson.png icon (alt="View Scanned Documents")
    indicates the row has scanned documents attached.
    """
    records = []

    grid = await page.query_selector("#ctl00_ContentPlaceHolder1_gridAuth")
    if not grid:
        logger.warning("Auth grid not found on page")
        return records

    rows = await grid.query_selector_all("tr")

    for row in rows:
        cells = await row.query_selector_all("td")
        if len(cells) < 7:
            # Header row or malformed row — skip
            continue

        try:
            start_date_str = (await cells[0].inner_text()).strip()
            end_date_str = (await cells[1].inner_text()).strip()
            vendor = (await cells[2].inner_text()).strip()
            payor = (await cells[3].inner_text()).strip()
            auth_number = (await cells[4].inner_text()).strip()
            record_no = (await cells[5].inner_text()).strip()
            services = (await cells[6].inner_text()).strip()

            # Check for scanned document icon (viewPerson.png)
            # This icon is ONLY present on rows that have scanned docs
            scanned_doc_icon = await row.query_selector(
                "img[src*='viewPerson'], img[alt='View Scanned Documents']"
            )
            has_scanned_doc = scanned_doc_icon is not None

            start_date = _parse_date(start_date_str)
            end_date = _parse_date(end_date_str)

            records.append({
                "start_date": start_date_str,
                "end_date": end_date_str,
                "start_date_parsed": start_date,
                "end_date_parsed": end_date,
                "vendor": vendor,
                "payor": payor,
                "auth_number": auth_number,
                "record_no": record_no,
                "services": services,
                "has_scanned_doc": has_scanned_doc,
            })

        except Exception as e:
            logger.warning("Failed to parse auth grid row", error=str(e))
            continue

    logger.info("Parsed auth grid", record_count=len(records))
    return records


def _find_matching_auth(
    auths: List[dict],
    claim_dos: date,
    service_code: str = "",
) -> Optional[dict]:
    """
    Find the auth record whose date range covers the claim DOS.

    If service_code is provided, prefer auths that include that code.
    If multiple auths cover the DOS, return the one with the narrowest range
    (most specific match).
    """
    matching = []

    for auth in auths:
        start = auth.get("start_date_parsed")
        end = auth.get("end_date_parsed")

        if not start or not end:
            continue

        if start <= claim_dos <= end:
            matching.append(auth)

    if not matching:
        return None

    # If service code provided, prefer matching service
    if service_code:
        service_matches = [
            a for a in matching
            if service_code.upper() in a.get("services", "").upper()
        ]
        if service_matches:
            matching = service_matches

    # Return the auth with the narrowest date range (most specific)
    matching.sort(
        key=lambda a: (
            (a["end_date_parsed"] - a["start_date_parsed"]).days
            if a["end_date_parsed"] and a["start_date_parsed"]
            else 9999
        )
    )

    return matching[0]


def _check_suspicious_duration(auth: dict) -> Optional[str]:
    """
    Flag auths with suspicious date ranges.
    MHSS auths > 3 months are unusual and should be reviewed.
    """
    start = auth.get("start_date_parsed")
    end = auth.get("end_date_parsed")

    if not start or not end:
        return None

    duration_days = (end - start).days

    if duration_days > 90:
        return (
            f"Auth date range is {duration_days} days "
            f"({auth['start_date']} - {auth['end_date']}). "
            f"MHSS auths > 3 months may be suspicious — verify with MCO."
        )

    return None


# ---------------------------------------------------------------------------
# Main function: check_lauris_authorization
# ---------------------------------------------------------------------------

async def check_lauris_authorization(
    consumer_uid: str,
    claim_dos: date,
    service_code: str = "",
) -> dict:
    """
    Check Lauris Authorization Management page for auth records covering
    the claim's date of service.

    Args:
        consumer_uid: Lauris consumer UID (e.g., "ID004665").
        claim_dos: Date of service to match against auth period.
        service_code: Optional HCPCS/CPT code to match (e.g., "H0046").

    Returns:
        {
            "found": bool,           # Were any matching auths found?
            "auth_number": str,      # Auth # from the matching record
            "start_date": str,       # Auth start date
            "end_date": str,         # Auth end date
            "payor": str,            # MCO name from grid
            "has_scanned_doc": bool, # viewPerson icon present?
            "services": str,         # Service codes and units
            "suspicious": str,       # Warning if date range > 3 months
            "all_auths": list,       # All auth records found for consumer
        }
    """
    logger.info(
        "Checking Lauris authorization",
        consumer_uid=consumer_uid,
        claim_dos=str(claim_dos),
        service_code=service_code,
    )

    empty_result = {
        "found": False,
        "auth_number": "",
        "start_date": "",
        "end_date": "",
        "payor": "",
        "has_scanned_doc": False,
        "services": "",
        "suspicious": "",
        "all_auths": [],
    }

    if DRY_RUN:
        logger.info("DRY_RUN: Would check Lauris auth", uid=consumer_uid)
        return empty_result

    try:
        async with LaurisSession() as lauris:
            base = lauris.login_url.rsplit("/", 1)[0]

            # Navigate to Authorization Management page
            await lauris.page.goto(
                f"{base}/admin_newui/authmanage.aspx",
                wait_until="domcontentloaded",
                timeout=20000,
            )
            await asyncio.sleep(2)

            # Fill consumer UID into the existing key value field
            await lauris.safe_fill(
                "#ctl00_ContentPlaceHolder1_txtExistingKeyVal",
                consumer_uid,
            )
            logger.info("Consumer UID entered", uid=consumer_uid)

            # Click Load Consumer
            await lauris.safe_click(
                "#ctl00_ContentPlaceHolder1_btnLoadConsumer"
            )
            await asyncio.sleep(3)

            # Click Search to populate the auth grid
            await lauris.safe_click(
                "#ctl00_ContentPlaceHolder1_btnAuth"
            )
            await asyncio.sleep(3)

            # Parse the grid
            all_auths = await _parse_auth_grid(lauris.page)

            if not all_auths:
                logger.info("No auth records found in Lauris", uid=consumer_uid)
                return empty_result

            # Find the auth covering the claim DOS
            match = _find_matching_auth(all_auths, claim_dos, service_code)

            if not match:
                logger.info(
                    "No auth covers claim DOS",
                    uid=consumer_uid,
                    claim_dos=str(claim_dos),
                    auth_count=len(all_auths),
                )
                return {
                    **empty_result,
                    "all_auths": all_auths,
                }

            # Check for suspicious duration
            suspicious = _check_suspicious_duration(match) or ""

            if suspicious:
                logger.warning(
                    "Suspicious auth duration",
                    uid=consumer_uid,
                    warning=suspicious,
                )

            logger.info(
                "Matching auth found in Lauris",
                uid=consumer_uid,
                auth_number=match["auth_number"],
                start=match["start_date"],
                end=match["end_date"],
                payor=match["payor"],
                has_scanned_doc=match["has_scanned_doc"],
            )

            return {
                "found": True,
                "auth_number": match["auth_number"],
                "start_date": match["start_date"],
                "end_date": match["end_date"],
                "payor": match["payor"],
                "has_scanned_doc": match["has_scanned_doc"],
                "services": match["services"],
                "suspicious": suspicious,
                "all_auths": all_auths,
            }

    except Exception as e:
        logger.error(
            "Lauris auth check failed",
            uid=consumer_uid,
            error=str(e),
        )
        await _try_screenshot(e)
        return empty_result


async def _try_screenshot(error: Exception):
    """Best-effort screenshot on failure — session may already be closed."""
    try:
        # LaurisSession is already exited at this point, so we can't screenshot
        # This is a placeholder for when we refactor to pass session in
        pass
    except Exception:
        pass


# ---------------------------------------------------------------------------
# High-level: check Lauris auth for a claim (handles UID lookup)
# ---------------------------------------------------------------------------

async def check_lauris_auth_for_claim(
    page: Page,
    claim_client_id: str,
    claim_dos: date,
    service_code: str = "",
) -> dict:
    """
    High-level wrapper that does UID lookup from Medicaid/record number
    then checks Lauris auth grid.

    Args:
        page: Playwright page with active Lauris session (for UID lookup).
        claim_client_id: Medicaid number (e.g., "710319037010").
        claim_dos: Date of service.
        service_code: Optional HCPCS/CPT code.

    Returns:
        Same dict as check_lauris_authorization(), with added "consumer_uid" key.
    """
    logger.info(
        "Looking up consumer UID for Lauris auth check",
        client_id=claim_client_id,
    )

    consumer_uid = await _lookup_uid_from_record_number(page, claim_client_id)

    if not consumer_uid:
        logger.warning(
            "Could not resolve Medicaid ID to Lauris UID",
            client_id=claim_client_id,
        )
        return {
            "found": False,
            "auth_number": "",
            "start_date": "",
            "end_date": "",
            "payor": "",
            "has_scanned_doc": False,
            "services": "",
            "suspicious": "",
            "all_auths": [],
            "consumer_uid": None,
        }

    result = await check_lauris_authorization(
        consumer_uid=consumer_uid,
        claim_dos=claim_dos,
        service_code=service_code,
    )
    result["consumer_uid"] = consumer_uid
    return result
