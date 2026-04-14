"""
actions/era_poster.py
---------------------
Automated ERA posting via Lauris EDI Results web page.

Flow per file (verified 2026-04-14):
  1. Select the file in the ddlEDIFiles dropdown
  2. Click "Post Selected File" — fires a native confirm() dialog
  3. Auto-accept the dialog (Playwright default would dismiss)
  4. Lauris navigates to AREntry.aspx?id=edi<N>&edircvd=<amount>&edifid=<id>
  5. Verify the URL's edircvd= matches the 835's BPR02 (amount mismatch
     means Claim.MD and Lauris have different copies of the file — skip)
  6. Fill Deposit Date (MM/DD/YYYY), Period (YYYYMM), Check Number (TRN02)
  7. Click btnStart "Post Payments" to actually commit
  8. Navigate back to EDI Results and verify the file_val is gone from the
     dropdown — this is the only reliable success signal
  9. Parse the 835 for PLB (Provider Level Balance) segments and either:
     - auto-write-off amounts <= $25 (typically ACH fees) in Lauris, or
     - flag larger amounts for human review via ClickUp

The EDI Results page is at: /ar/ClosedBillingEDIResults.aspx
Files are named: era_{payerid}_{eraid}.x12 (- MM/DD/YYYY)
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
from sources.claimmd_api import ClaimMDAPI

logger = get_logger("era_poster")
clickup = ClickUpLogger()

# Track which ERA files have been posted to Lauris (persistent)
POSTED_ERAS_FILE = Path("data/posted_eras.json")

# Explicit "do not attempt to post" list — files known to silently fail because
# Lauris's clearinghouse has different 835 content than Claim.MD, plus phantom
# -1 duplicates that Lauris redelivers after every successful post. Populated
# from the 2026-04-14 debugging session. Skipping these avoids wasted retries
# and false ClickUp failure tasks.
UNPOSTABLE_ERAS_FILE = Path("data/unpostable_eras.json")

# PLB adjustments (ACH fees, etc.) at or below this amount are auto-written-off.
# Anything above this threshold is flagged for human review.
PLB_AUTO_WRITEOFF_MAX = 25.00

# Directory where downloaded 835 files are cached (populated by era_manager.py
# when it downloads from Claim.MD). PLB parsing reads from here.
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


def _load_unpostable_eras() -> dict:
    """Load the {file_val: reason} map of ERA files to skip entirely."""
    if not UNPOSTABLE_ERAS_FILE.exists():
        return {}
    try:
        with open(UNPOSTABLE_ERAS_FILE) as f:
            data = json.load(f)
    except Exception:
        return {}
    out = {}
    for entry in data.get("ignored", []):
        fv = entry.get("file_val", "")
        if fv:
            out[fv] = entry.get("reason", "") or entry.get("category", "unpostable")
    return out


def _parse_era_id_from_filename(fname: str) -> str:
    """'era_54154_71905665.x12 - 03/25/2026' -> '71905665'.

    Also handles 'era_71905665.x12' (no payer prefix, used for Claim.MD-staged
    files in ERA_DOWNLOAD_DIR)."""
    m = re.search(r"era[_-](?:[A-Za-z0-9]+_)?(\d+)", fname)
    return m.group(1) if m else ""


# Backwards-compat alias — some PLB code paths use the older name
_extract_era_id_from_filename = _parse_era_id_from_filename


def _parse_835_metadata(content: str) -> dict:
    """Extract BPR16 (check/EFT date CCYYMMDD), BPR02 (total), TRN02 (trace)."""
    out = {"bpr16": "", "trn02": "", "bpr02": ""}
    for seg in content.split("~"):
        parts = seg.strip().split("*")
        if not parts:
            continue
        if parts[0] == "BPR":
            if len(parts) >= 17:
                out["bpr16"] = parts[16].strip()
            if len(parts) >= 3:
                out["bpr02"] = parts[2].strip()
        elif parts[0] == "TRN" and len(parts) >= 3 and not out["trn02"]:
            out["trn02"] = parts[2].strip()
    return out


def _ccyymmdd_to_mmddyyyy(s: str) -> str:
    """20260325 -> 03/25/2026"""
    s = (s or "").strip()
    if len(s) != 8 or not s.isdigit():
        return ""
    return f"{s[4:6]}/{s[6:8]}/{s[:4]}"


def _ccyymmdd_to_yyyymm(s: str) -> str:
    """20260325 -> 202603 (Lauris period format, not MM/YYYY)"""
    s = (s or "").strip()
    if len(s) != 8 or not s.isdigit():
        return ""
    return f"{s[:4]}{s[4:6]}"


def _parse_plb_adjustments(era_id: str) -> list[dict]:
    """
    Parse PLB (Provider Level Balance) segments from a downloaded 835 file.
    Returns a list of dicts: {check_number, amount, reason_code, npi}.

    PLB format: PLB*npi*fiscal_year_end*reason_code:reference*amount
    Common in ERAs where the MCO deducts an ACH fee from the payment.
    Reads from ERA_DOWNLOAD_DIR (populated by era_manager.download_and_stage_eras).
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
    for seg in re.split(r"~", content):
        seg = seg.strip()
        if not seg.startswith("PLB"):
            continue
        fields = seg.split("*")
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

    # Navigate back to EDI Results page so subsequent ERA postings work
    await lauris.page.goto(
        f"{base_url}/{EDI_RESULTS_PATH}",
        wait_until="domcontentloaded",
        timeout=20000,
    )
    await asyncio.sleep(3)


async def post_pending_eras() -> dict:
    """
    Post all pending ERA/EDI files in Lauris via the web interface.
    After each successful post, parses PLB segments and either auto-writes-off
    amounts <= $25 or flags larger amounts for human review.
    """
    result = {
        "posted": 0,
        "skipped_irregular": 0,
        "skipped_old": 0,
        "skipped_already_posted": 0,
        "skipped_unpostable": 0,
        "errors": 0,
        "plb_writeoffs": 0,
        "plb_flagged": 0,
        "posted_files": [],
        "irregular_files": [],
        "failed_files": [],
        "plb_details": [],
    }

    if DRY_RUN:
        logger.info("DRY_RUN: Would post pending ERAs")
        return result

    try:
        async with LaurisSession() as lauris:
            base = lauris.login_url.rsplit("/", 1)[0]

            # Lauris fires a native JavaScript confirm() dialog after clicking
            # "Post Selected File": 'This will send the items to AR Posting.'
            # Without accepting it, Playwright's default is to dismiss → the
            # form never submits and the post silently fails. Accept all dialogs.
            lauris.page.on(
                "dialog",
                lambda d: asyncio.create_task(d.accept()),
            )

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

            # Load explicit ignore list (unpostable ERAs + known phantoms)
            unpostable = _load_unpostable_eras()
            logger.info(
                "Loaded unpostable ERA ignore list",
                count=len(unpostable),
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

                # Check explicit ignore list (known-unpostable + phantom duplicates)
                if val in unpostable:
                    result["skipped_unpostable"] += 1
                    logger.info(
                        "Skipping unpostable ERA",
                        file=text,
                        file_val=val,
                        reason=unpostable[val][:120],
                    )
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
            api = ClaimMDAPI()

            # Post each file
            for file_val, file_name in files_to_post:
                try:
                    # Look up the 835 metadata needed to fill the AREntry form.
                    # Lauris requires Deposit Date, Period, and Check Number to
                    # commit a posting — all of which are in the 835 itself.
                    era_id = _parse_era_id_from_filename(file_name)
                    check_date = period = check_no = ""
                    meta: dict = {}
                    if era_id and api.key:
                        try:
                            content = await api.download_era_835(era_id)
                            if content:
                                meta = _parse_835_metadata(content)
                                check_date = _ccyymmdd_to_mmddyyyy(meta["bpr16"])
                                period     = _ccyymmdd_to_yyyymm(meta["bpr16"])
                                check_no   = meta["trn02"]
                        except Exception as e:
                            logger.warning(
                                "Failed to download 835 for metadata",
                                era_id=era_id, error=str(e)[:120],
                            )

                    if not (check_date and period and check_no):
                        logger.error(
                            "Missing 835 metadata — cannot post",
                            file=file_name, era_id=era_id,
                            have_date=bool(check_date),
                            have_period=bool(period),
                            have_check=bool(check_no),
                        )
                        result["errors"] += 1
                        result["failed_files"].append({
                            "file_name": file_name,
                            "file_val": file_val,
                            "reason": f"missing_835_metadata (era_id={era_id})",
                            "screenshot": "",
                        })
                        await lauris.page.goto(
                            f"{base}/{EDI_RESULTS_PATH}",
                            wait_until="domcontentloaded",
                            timeout=20000,
                        )
                        await asyncio.sleep(2)
                        continue

                    # Select the file from dropdown
                    await lauris.page.select_option(
                        "select[name='ddlEDIFiles']", value=file_val
                    )
                    await asyncio.sleep(0.5)

                    # Click "Post Selected File" — fires a native confirm()
                    # dialog which our dialog handler auto-accepts. Lauris
                    # then navigates to AREntry.aspx with the paid amount
                    # in the URL (?edircvd=<dollars>).
                    await lauris.page.click(
                        "input[name='btnPostFile']", timeout=10000
                    )
                    try:
                        await lauris.page.wait_for_load_state(
                            "networkidle", timeout=15000
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(1)

                    # Extract the paid amount from the URL Lauris redirected to.
                    post_url = lauris.page.url
                    paid_amount = 0.0
                    m = re.search(r"edircvd=([0-9.]+)", post_url)
                    if m:
                        try:
                            paid_amount = float(m.group(1))
                        except ValueError:
                            paid_amount = 0.0

                    # Amount-mismatch pre-check: if Lauris's edircvd differs
                    # from the Claim.MD 835's BPR02 by more than a penny, the
                    # Claim.MD 835 we have is a *different* file than Lauris's
                    # internal copy. This is typically caused by PLB (Provider
                    # Level Balance) segments like ACH fees — era_manager's
                    # PLB stripping should prevent this, but if it slips
                    # through we fail loudly so Desiree can investigate.
                    try:
                        bpr02 = float(meta.get("bpr02") or 0)
                    except ValueError:
                        bpr02 = 0.0
                    if paid_amount and bpr02 and abs(paid_amount - bpr02) > 0.01:
                        logger.error(
                            "Amount mismatch — Lauris and Claim.MD "
                            "have different 835 amounts; cannot safely post "
                            "(should have been caught by PLB stripping — investigate)",
                            file=file_name,
                            lauris_amount=f"${paid_amount:,.2f}",
                            claimmd_amount=f"${bpr02:,.2f}",
                            delta=f"${abs(paid_amount - bpr02):,.2f}",
                            era_id=era_id,
                        )
                        result["errors"] += 1
                        result["failed_files"].append({
                            "file_name": file_name,
                            "file_val": file_val,
                            "reason": (
                                f"amount_mismatch (Lauris=${paid_amount:,.2f}, "
                                f"Claim.MD=${bpr02:,.2f}, era_id={era_id}) — "
                                f"add to data/unpostable_eras.json or investigate "
                                f"PLB stripping in era_manager.py"
                            ),
                            "screenshot": "",
                        })
                        await lauris.page.goto(
                            f"{base}/{EDI_RESULTS_PATH}",
                            wait_until="domcontentloaded",
                            timeout=20000,
                        )
                        await asyncio.sleep(2)
                        continue

                    # On AREntry.aspx, fill Deposit Date / Period / Check
                    # Number and click Post Payments to commit the post.
                    # The date field opens a jQuery datepicker on focus;
                    # set it directly via the DOM to avoid the picker
                    # overlay stealing subsequent clicks.
                    await lauris.page.evaluate(
                        """([date]) => {
                            const el = document.querySelector(
                                "input[name='txtCheckDate']"
                            );
                            if (el) {
                                el.value = date;
                                el.dispatchEvent(
                                    new Event('change', {bubbles: true})
                                );
                            }
                        }""",
                        [check_date],
                    )
                    await lauris.page.fill(
                        "input[name='txtPeriod']", period
                    )
                    await lauris.page.fill(
                        "input[name='txtEDICheckNo']", check_no
                    )
                    await asyncio.sleep(0.3)
                    await lauris.page.click(
                        "input[name='btnStart']", timeout=10000
                    )
                    try:
                        await lauris.page.wait_for_load_state(
                            "networkidle", timeout=30000
                        )
                    except Exception:
                        pass
                    await asyncio.sleep(2)

                    # Navigate back to the EDI Results page so we can verify
                    # the file is gone and prepare for the next iteration.
                    await lauris.page.goto(
                        f"{base}/{EDI_RESULTS_PATH}",
                        wait_until="domcontentloaded",
                        timeout=20000,
                    )
                    await asyncio.sleep(2)

                    # Definitive success check: the target file_val must no
                    # longer be in the dropdown. This replaces the brittle
                    # page-text scan (Lauris fails silently on confirm-dismiss).
                    still_present_el = await lauris.page.query_selector(
                        f"select[name='ddlEDIFiles'] option[value='{file_val}']"
                    )
                    if still_present_el is not None:
                        screenshot_path = ""
                        try:
                            screenshot_path = (
                                f"/tmp/claims_work/era_post_error_{file_val}.png"
                            )
                            await lauris.page.screenshot(path=screenshot_path)
                        except Exception:
                            screenshot_path = ""
                        logger.error(
                            "ERA post failed — file still in dropdown after click",
                            file=file_name,
                            post_url=post_url,
                            screenshot=screenshot_path,
                        )
                        result["errors"] += 1
                        result["failed_files"].append({
                            "file_name": file_name,
                            "file_val": file_val,
                            "reason": (
                                f"file_still_in_dropdown_after_click "
                                f"(post_url={post_url})"
                            ),
                            "screenshot": screenshot_path,
                        })
                        continue

                    result["posted"] += 1
                    result["posted_files"].append(
                        f"{file_name} → ${paid_amount:,.2f}" if paid_amount
                        else file_name
                    )
                    already_posted_local.add(file_val)
                    logger.info(
                        "ERA posted successfully",
                        file=file_name,
                        paid=f"${paid_amount:,.2f}" if paid_amount else "?",
                    )

                    # After a successful post, parse the 835 for PLB (Provider
                    # Level Balance) segments — typically ACH fees the MCO
                    # deducted from the payment — and either auto-write-off
                    # small amounts or flag larger ones for human review.
                    if era_id:
                        plb_adjustments = _parse_plb_adjustments(era_id)
                        for adj in plb_adjustments:
                            if adj["amount"] <= PLB_AUTO_WRITEOFF_MAX:
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
                    result["failed_files"].append({
                        "file_name": file_name,
                        "file_val": file_val,
                        "reason": f"exception: {str(e)[:200]}",
                        "screenshot": "",
                    })

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
        if k not in ("posted_files", "irregular_files",
                     "failed_files", "plb_details")
    })
    return result
