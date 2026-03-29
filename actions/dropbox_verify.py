"""
actions/dropbox_verify.py
--------------------------
Verify portal-submitted service authorizations were saved to Dropbox.

Per the Complete Framework:
  When auth was submitted via portal (not fax), check Dropbox for
  the saved confirmation file. If not found, flag as CRITICAL GAP
  and create ClickUp task for NaTarsha.

Dropbox path: Chesapeake LCI > Service Authorizations > [year] > [client folder]
"""
from __future__ import annotations

import os
from datetime import date
from pathlib import Path
from typing import Optional

from config.settings import DRY_RUN
from logging_utils.logger import get_logger

logger = get_logger("dropbox_verify")

# Base path for service authorizations in Dropbox
DROPBOX_AUTH_BASE = Path(os.path.expanduser(
    "~/Library/CloudStorage/Dropbox-LifeConsultantsInc/"
    "Chesapeake LCI/Service Authorizations"
))


def find_auth_in_dropbox(
    client_name: str,
    mco: str = "",
    year: str = "",
) -> Optional[str]:
    """
    Search Dropbox for a saved authorization confirmation file.
    Returns the file path if found, None if not.
    """
    if not year:
        year = str(date.today().year)

    search_dir = DROPBOX_AUTH_BASE
    if not search_dir.exists():
        # Try alternate paths
        for alt in [
            DROPBOX_AUTH_BASE.parent / "Auth Files",
            DROPBOX_AUTH_BASE.parent / "Authorizations",
        ]:
            if alt.exists():
                search_dir = alt
                break

    if not search_dir.exists():
        logger.warning("Dropbox auth directory not found",
                        path=str(DROPBOX_AUTH_BASE))
        return None

    # Search by client name in folder names and file names
    name_parts = client_name.lower().split()
    if not name_parts:
        return None

    # Look in year folder first
    year_dir = search_dir / year
    search_dirs = [year_dir, search_dir] if year_dir.exists() else [search_dir]

    for sdir in search_dirs:
        try:
            for root, dirs, files in os.walk(str(sdir)):
                # Check folder names
                folder_name = os.path.basename(root).lower()
                if any(part in folder_name for part in name_parts if len(part) > 2):
                    # Found a folder matching client name
                    # Check for auth files inside
                    for f in files:
                        if f.lower().endswith(('.pdf', '.png', '.jpg', '.doc', '.docx')):
                            full_path = os.path.join(root, f)
                            logger.info("Auth file found in Dropbox",
                                        client=client_name,
                                        path=full_path)
                            return full_path

                # Also check file names directly
                for f in files:
                    f_lower = f.lower()
                    if any(part in f_lower for part in name_parts if len(part) > 2):
                        if mco and mco.lower() in f_lower:
                            full_path = os.path.join(root, f)
                            logger.info("Auth file found by name match",
                                        client=client_name,
                                        path=full_path)
                            return full_path
        except Exception as e:
            logger.warning("Dropbox search error", dir=str(sdir), error=str(e))

    logger.info("No auth file found in Dropbox", client=client_name)
    return None


async def verify_dropbox_auth(
    client_name: str,
    mco: str,
    claim_id: str = "",
) -> dict:
    """
    Verify that a portal-submitted auth was saved to Dropbox.
    Returns dict with {found: bool, path: str, gap_logged: bool}.
    """
    result = {
        "found": False,
        "path": "",
        "gap_logged": False,
    }

    path = find_auth_in_dropbox(client_name, mco)

    if path:
        result["found"] = True
        result["path"] = path
        logger.info("Dropbox auth verified",
                     client=client_name, path=path)
    else:
        result["gap_logged"] = True
        logger.warning("CRITICAL GAP: Auth not saved to Dropbox",
                         client=client_name, mco=mco)

        # Log gap and create ClickUp task for NaTarsha
        if not DRY_RUN:
            try:
                from reporting.gap_report import GapReporter, GapCategory
                gr = GapReporter()
                gr.log_gap(
                    claim_id=claim_id,
                    client_name=client_name,
                    mco=mco,
                    program="",
                    denial_type="no_auth_portal_no_dropbox",
                    gap_category=GapCategory.AUTH_NOT_SAVED_DROPBOX,
                    dollar_amount=0,
                    resolution="Flagged for NaTarsha — Dropbox save required",
                    status="pending",
                )
                gr.close()

                # Create ClickUp task for NaTarsha
                from actions.clickup_tasks import ClickUpTaskCreator
                task_creator = ClickUpTaskCreator()
                await task_creator.create_natarsha_dropbox_task([
                    f"{client_name} ({mco})"
                ])
            except Exception as e:
                logger.warning("Gap logging/task creation failed", error=str(e))

    return result
