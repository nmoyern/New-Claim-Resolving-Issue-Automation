"""
actions/era_manager.py
----------------------
ERA management: download from Claim.MD API, organize by MCO/date,
notify team for Lauris desktop upload, and track upload status.

The Lauris Billing Center is a Windows desktop app — ERA upload cannot
be automated via the web portal. This module manages the ERA files
and creates a ClickUp task when new ERAs need manual upload.

Flow:
  1. Download 835 files from Claim.MD API (automated)
  2. Organize into Dropbox folder by MCO and date (automated)
  3. Create ClickUp task for manual Lauris upload (automated)
  4. Track which ERAs have been uploaded (via status file)
"""
from __future__ import annotations

import json
import os
import re
import shutil
from datetime import date
from pathlib import Path
from typing import List, Tuple

from config.settings import DRY_RUN
from config.entities import get_entity_by_npi
from sources.claimmd_api import ClaimMDAPI, PAYER_MCO_MAP
from lauris.billing import classify_era
from config.models import ERA, MCO, Program
from logging_utils.logger import get_logger, ClickUpLogger

logger = get_logger("era_manager")
clickup = ClickUpLogger()


def _strip_plb_from_835(file_path: str) -> list[dict]:
    """
    Strip PLB (Provider Level Balance) segments from an 835 file and fix
    the BPR total to match the sum of CLP payments. PLB segments contain
    ACH fees that cause Lauris to silently reject the ERA posting.

    Returns a list of PLB adjustments found (each a dict with
    npi, reason_code, check_number, amount).
    """
    try:
        content = Path(file_path).read_text()
    except Exception:
        return []

    segments = content.split("~")
    clean_segments = []
    plb_adjustments = []
    total_plb_amount = 0.0

    for seg in segments:
        seg_stripped = seg.strip()
        if seg_stripped.startswith("PLB"):
            fields = seg_stripped.split("*")
            if len(fields) >= 5:
                npi = fields[1]
                reason_ref = fields[3]
                try:
                    amount = abs(float(fields[4]))
                except ValueError:
                    amount = 0.0
                reason_code = reason_ref.split(":")[0] if ":" in reason_ref else reason_ref
                check_ref = reason_ref.split(":")[1] if ":" in reason_ref else ""
                plb_adjustments.append({
                    "npi": npi,
                    "reason_code": reason_code,
                    "check_number": check_ref,
                    "amount": amount,
                })
                total_plb_amount += amount
                logger.info(
                    "Stripped PLB segment from 835",
                    file=file_path,
                    amount=amount,
                    check=check_ref,
                )
            continue
        clean_segments.append(seg)

    if not plb_adjustments:
        return []

    # Fix BPR total: add back the PLB amount so BPR matches CLP totals
    clean_content = "~".join(clean_segments)
    bpr_match = re.search(r"(BPR\*[A-Z]\*)([0-9.]+)(\*)", clean_content)
    if bpr_match:
        old_total = float(bpr_match.group(2))
        new_total = old_total + total_plb_amount
        clean_content = clean_content.replace(
            bpr_match.group(0),
            f"{bpr_match.group(1)}{new_total:.2f}{bpr_match.group(3)}",
            1,
        )
        logger.info(
            "Fixed BPR total in 835",
            old=f"${old_total:.2f}",
            new=f"${new_total:.2f}",
        )

    Path(file_path).write_text(clean_content)
    return plb_adjustments


async def _create_plb_writeoff_task(
    plb_adjustments: list[dict],
    era_id: str,
    mco_name: str,
    program_name: str,
) -> None:
    """Create a ClickUp task assigned to Desiree for PLB write-off."""
    from actions.clickup_tasks import (
        ClickUpTaskCreator,
        MEMBER_DESIREE,
        PRIORITY_HIGH,
        _next_business_day,
    )

    tc = ClickUpTaskCreator()

    total_plb = sum(a["amount"] for a in plb_adjustments)
    detail_lines = []
    for adj in plb_adjustments:
        detail_lines.append(
            f"  - ${adj['amount']:.2f} (check {adj['check_number']}, "
            f"reason {adj['reason_code']}, NPI {adj['npi']})"
        )
    detail_text = "\n".join(detail_lines)

    due = _next_business_day()

    task_name = (
        f"Write off PLB fee ${total_plb:.2f} - "
        f"{mco_name} ERA {era_id}"
    )
    description = (
        f"ERA {era_id} ({mco_name}, {program_name}) contained a "
        f"Provider Level Balance (PLB) adjustment of ${total_plb:.2f}.\n\n"
        f"The PLB segment was automatically stripped from the 835 file "
        f"so Lauris could process the ERA. The following amount(s) need "
        f"to be written off in Lauris Billing Center:\n\n"
        f"{detail_text}\n\n"
        f"Total to write off: ${total_plb:.2f}\n\n"
        f"This is typically an ACH fee deducted by the MCO from the "
        f"payment deposit.\n\n"
        f"#AUTO #{date.today().strftime('%m/%d/%y')}"
    )

    task_id = await tc.create_task(
        list_id=tc.list_id,
        name=task_name,
        description=description,
        assignees=[MEMBER_DESIREE],
        due_date=due,
        priority=PRIORITY_HIGH,
    )

    if task_id:
        logger.info(
            "PLB write-off ClickUp task created",
            task_id=task_id,
            era_id=era_id,
            amount=total_plb,
            due=str(due.date()),
        )
    else:
        logger.error(
            "Failed to create PLB write-off ClickUp task",
            era_id=era_id,
        )

# Where to stage ERA files for manual Lauris upload
ERA_STAGING_DIR = Path(os.path.expanduser(
    "~/Library/CloudStorage/Dropbox-LifeConsultantsInc/"
    "Chesapeake LCI/AR Reports/ERA Files/Pending Upload"
))

# Track which ERAs have been processed
ERA_STATUS_FILE = Path("data/era_status.json")


def _load_era_status() -> dict:
    """Load ERA processing status from disk."""
    if ERA_STATUS_FILE.exists():
        with open(ERA_STATUS_FILE) as f:
            return json.load(f)
    return {"uploaded": [], "pending": [], "irregular": []}


def _save_era_status(status: dict):
    """Save ERA processing status to disk."""
    ERA_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(ERA_STATUS_FILE, "w") as f:
        json.dump(status, f, indent=2)


async def download_and_stage_eras() -> dict:
    """
    Download today's ERAs from Claim.MD API, classify them,
    and stage standard ones for manual Lauris upload.

    Returns dict with counts: downloaded, staged, irregular, already_processed.
    """
    api = ClaimMDAPI()
    if not api.key:
        logger.warning("No Claim.MD API key — cannot download ERAs")
        return {"downloaded": 0, "staged": 0, "irregular": 0, "already_processed": 0}

    # Get ALL unprocessed ERAs (era_status.json tracks which have been processed)
    era_list = await api.get_era_list(new_only=True)
    logger.info(f"Found {len(era_list)} unprocessed ERAs")

    if not era_list:
        return {"downloaded": 0, "staged": 0, "irregular": 0, "already_processed": 0}

    # Load status to avoid re-processing
    status = _load_era_status()
    processed_ids = set(status.get("uploaded", []) + status.get("pending", []))

    downloaded = 0
    staged = 0
    irregular_count = 0
    plb_stripped = 0
    already = 0
    irregular_list = []
    staged_list = []

    download_dir = Path("/tmp/claims_work/eras")
    download_dir.mkdir(parents=True, exist_ok=True)

    for era_info in era_list:
        era_id = str(era_info.get("eraid", ""))
        if not era_id:
            continue

        if era_id in processed_ids:
            already += 1
            continue

        # Download 835 file
        save_path = str(download_dir / f"era_{era_id}.835")
        content = await api.download_era_835(era_id, save_path)
        if not content:
            continue
        downloaded += 1

        # Classify
        payer_id = era_info.get("payerid", "")
        payer_name = era_info.get("payer_name", "")
        npi = era_info.get("prov_npi", "")
        amount = float(era_info.get("paid_amount", "0") or "0")

        # Infer MCO and program
        from sources.claimmd import _parse_mco
        mco_str = PAYER_MCO_MAP.get(payer_id, payer_name)
        mco = _parse_mco(mco_str)

        entity = get_entity_by_npi(npi)
        program = entity.program if entity else Program.UNKNOWN

        era_obj = ERA(
            era_id=era_id, mco=mco, program=program,
            payment_date=date.today(), total_amount=amount,
            file_path=save_path,
        )
        era_type = classify_era(era_obj)

        if era_type != "standard":
            irregular_count += 1
            irregular_list.append(f"{era_id} ({era_type}, ${amount:.2f})")
            status.setdefault("irregular", []).append(era_id)
            logger.info("Irregular ERA flagged", era_id=era_id, type=era_type)
            continue

        # Strip PLB segments (ACH fees) before the file reaches Lauris.
        # PLB causes Lauris to silently reject ERA posting due to amount mismatch.
        # Creates a ClickUp task for Desiree to write off the amount.
        plb_adjustments = _strip_plb_from_835(save_path)
        if plb_adjustments:
            total_plb = sum(a["amount"] for a in plb_adjustments)
            logger.info(
                "PLB stripped from ERA",
                era_id=era_id,
                plb_count=len(plb_adjustments),
                total=f"${total_plb:.2f}",
            )
            await _create_plb_writeoff_task(
                plb_adjustments, era_id,
                mco.value, program.value,
            )
            plb_stripped += 1

        # Stage for manual Lauris upload
        if not DRY_RUN:
            staged_dir = ERA_STAGING_DIR / date.today().strftime("%Y-%m-%d")
            staged_dir.mkdir(parents=True, exist_ok=True)
            dest = staged_dir / f"{mco.value}_{program.value}_{era_id}.835"
            shutil.copy2(save_path, str(dest))
            logger.info("ERA staged for Lauris upload",
                        era_id=era_id, dest=str(dest))

        staged += 1
        staged_list.append(f"{era_id} ({mco.value}, ${amount:.2f})")
        status.setdefault("pending", []).append(era_id)

    # Save status
    _save_era_status(status)

    # Post ClickUp notification if there are ERAs to upload
    if staged_list and not DRY_RUN:
        era_summary = "\n".join(f"  - {e}" for e in staged_list)
        irregular_summary = ""
        if irregular_list:
            irregular_summary = (
                f"\n\nIrregular ERAs (manual handling required):\n"
                + "\n".join(f"  - {e}" for e in irregular_list)
            )

        await clickup.post_comment(
            f"ERA Upload Required — {staged} new ERA(s) ready for Lauris upload.\n\n"
            f"Files staged at: {ERA_STAGING_DIR / date.today().strftime('%Y-%m-%d')}\n\n"
            f"Standard ERAs to upload:\n{era_summary}"
            f"{irregular_summary}\n\n"
            f"Please upload these 835 files to Lauris Billing Center (desktop app).\n"
            f"#AUTO #{date.today().strftime('%m/%d/%y')}"
        )

    result = {
        "downloaded": downloaded,
        "staged": staged,
        "irregular": irregular_count,
        "plb_stripped": plb_stripped,
        "already_processed": already,
    }
    logger.info("ERA staging complete", **result)
    return result
