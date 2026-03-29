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
import shutil
from datetime import date
from pathlib import Path
from typing import List, Tuple

from config.settings import DRY_RUN
from sources.claimmd_api import ClaimMDAPI, PAYER_MCO_MAP
from lauris.billing import classify_era
from config.models import ERA, MCO, Program
from logging_utils.logger import get_logger, ClickUpLogger

logger = get_logger("era_manager")
clickup = ClickUpLogger()

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

        program = Program.UNKNOWN
        if npi == "1437871753":
            program = Program.MARYS_HOME
        elif npi == "1700297447":
            program = Program.NHCS
        elif npi == "1306491592":
            program = Program.KJLN

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
        "already_processed": already,
    }
    logger.info("ERA staging complete", **result)
    return result
