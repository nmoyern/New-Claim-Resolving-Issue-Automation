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


async def post_pending_eras() -> dict:
    """
    Post all pending ERA/EDI files in Lauris via the web interface.

    Returns dict with counts: posted, skipped_irregular, already_posted, errors.
    """
    result = {
        "posted": 0,
        "skipped_irregular": 0,
        "skipped_old": 0,
        "skipped_already_posted": 0,
        "errors": 0,
        "posted_files": [],
        "irregular_files": [],
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

                await clickup.post_comment(
                    f"ERA Posting Complete — {result['posted']} ERA(s) "
                    f"posted to Lauris via EDI Results.\n\n"
                    f"Posted:\n{posted_list}"
                    f"{irregular_note}\n\n"
                    f"#AUTO #{date.today().strftime('%m/%d/%y')}"
                )

    except Exception as e:
        logger.error("ERA posting failed", error=str(e))
        result["errors"] += 1

    logger.info("ERA posting complete", **{
        k: v for k, v in result.items()
        if k not in ("posted_files", "irregular_files")
    })
    return result
