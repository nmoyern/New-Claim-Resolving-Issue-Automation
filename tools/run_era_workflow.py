"""Run the full ERA workflow (download+stage from Claim.MD, post to Lauris)
and, on any posting failure, create a ClickUp task assigned to Desiree
Whitehead with the per-file error details AND the relevant 835 files attached
so she has everything she needs to post manually.

Wraps actions.era_manager.download_and_stage_eras and
actions.era_poster.post_pending_eras — designed to be run ad-hoc (from the
command line) without touching the full orchestrator.

Usage:
    cd <project root>
    DRY_RUN=false python3 tools/run_era_workflow.py
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from datetime import date
from pathlib import Path

import aiohttp
import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from actions.era_manager import download_and_stage_eras, ERA_STAGING_DIR  # noqa: E402
from actions.era_poster import post_pending_eras  # noqa: E402
from sources.claimmd_api import ClaimMDAPI  # noqa: E402

# Desiree Whitehead — ClickUp user ID for ERA-posting failure notifications
DESIREE_USER_ID = 30050728
CLICKUP_LIST_ID = os.environ.get("CLICKUP_LAURIS_LIST_ID", "187219903")
CLICKUP_TOKEN = os.environ.get("CLICKUP_API_TOKEN", "")


def _parse_era_id_from_filename(fname: str) -> str:
    """'era_54154_71905665.x12 - 03/25/2026' -> '71905665'."""
    m = re.search(r"era[_-](?:[A-Z0-9]+_)?(\d+)", fname)
    return m.group(1) if m else ""


async def _attach_835_to_task(task_id: str, era_id: str, display_name: str) -> bool:
    """Download the 835 from Claim.MD and attach it to a ClickUp task."""
    api = ClaimMDAPI()
    try:
        content = await api.download_era_835(era_id)
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ download_era_835({era_id}) failed: {e}")
        return False
    if not content:
        print(f"  ✗ Claim.MD returned nothing for era {era_id}")
        return False

    tmp = Path(f"/tmp/{display_name}")
    tmp.write_text(content)

    url = f"https://api.clickup.com/api/v2/task/{task_id}/attachment"
    headers = {"Authorization": CLICKUP_TOKEN}
    try:
        with open(tmp, "rb") as f:
            r = requests.post(
                url,
                headers=headers,
                files={"attachment": (display_name, f, "application/edi-x12")},
                timeout=60,
            )
    except Exception as e:  # noqa: BLE001
        print(f"  ✗ ClickUp upload exception for {display_name}: {e}")
        return False
    if r.status_code in (200, 201):
        print(f"  ✓ attached {display_name}")
        return True
    print(f"  ✗ ClickUp attach failed ({r.status_code}): {r.text[:200]}")
    return False


async def _create_desiree_task(title: str, body: str) -> dict:
    url = f"https://api.clickup.com/api/v2/list/{CLICKUP_LIST_ID}/task"
    headers = {"Authorization": CLICKUP_TOKEN, "Content-Type": "application/json"}
    payload = {
        "name": title,
        "description": body,
        "assignees": [DESIREE_USER_ID],
        "priority": 2,  # high
        "notify_all": True,
    }
    async with aiohttp.ClientSession() as s:
        async with s.post(url, json=payload, headers=headers) as r:
            body_text = await r.text()
            if r.status in (200, 201):
                return {"ok": True, "task": json.loads(body_text)}
            return {"ok": False, "status": r.status, "body": body_text[:500]}


def _build_task_body(stage_result: dict, post_result: dict) -> tuple[str, str]:
    today = date.today().strftime("%Y-%m-%d")
    staging_dir = ERA_STAGING_DIR / today
    failed = post_result.get("failed_files", [])
    errors = post_result.get("errors", 0)

    title = f"ERA Posting Failures — {today} — {errors} file(s) need manual review"
    lines = [
        "One or more ERA files failed to post to Lauris via EDI Results automation.",
        "",
        "**Dropbox staging folder (today's 835 files from Claim.MD):**",
        f"`{staging_dir}`",
        "",
        "**Counts from this run:**",
        f"- Downloaded from Claim.MD: {stage_result.get('downloaded', 0)}",
        f"- Staged to Dropbox:        {stage_result.get('staged', 0)}",
        f"- Successfully posted:      {post_result.get('posted', 0)}",
        f"- **Failed to post:        {errors}**",
        f"- Skipped already posted:   {post_result.get('skipped_already_posted', 0)}",
        f"- Skipped unpostable:       {post_result.get('skipped_unpostable', 0)}",
        f"- Skipped irregular:        {post_result.get('skipped_irregular', 0)}",
        f"- Skipped old (>1yr):       {post_result.get('skipped_old', 0)}",
        "",
        "---",
        "",
        "## Files that failed to post",
        "",
    ]

    if failed:
        for i, f in enumerate(failed, 1):
            lines.append(f"### {i}. {f.get('file_name', '(unnamed)')}")
            lines.append(f"- **Lauris EDI file value:** `{f.get('file_val', '')}`")
            lines.append(f"- **Reason:** {f.get('reason', 'unknown')}")
            if f.get("screenshot"):
                lines.append(f"- **Screenshot:** `{f['screenshot']}`")
            lines.append("- **835 file:** attached to this task")
            lines.append("")
    else:
        lines.append("_No per-file details were captured; check the log._")
        lines.append("")

    if post_result.get("irregular_files"):
        lines.append("---")
        lines.append("")
        lines.append("## Irregular ERAs (auto-skipped, need manual handling)")
        lines.append("")
        for f in post_result["irregular_files"]:
            lines.append(f"- {f}")
        lines.append("")

    lines.extend([
        "---",
        "",
        "**Next steps for Desiree:**",
        "1. Open Lauris → AR Reports → EDI Results",
        "2. Find each file listed above in the dropdown and click Post Selected File",
        "3. On AREntry, fill Deposit Date / Period (YYYYMM) / Check Number and click Post Payments",
        "4. If the automation keeps failing on a file, reference the attached 835 to see the internal content",
        "",
        f"Logs: `{PROJECT_ROOT / 'logs' / 'automation.log'}`",
        "Screenshots: `/tmp/claims_work/era_post_error_*.png`",
        "",
        "_Auto-created by tools/run_era_workflow.py._",
    ])
    return title, "\n".join(lines)


async def main():
    print("=" * 70)
    print(f"ERA Workflow — {date.today().isoformat()}")
    print("=" * 70)

    print("\nStep 1: download_and_stage_eras() ...")
    try:
        stage_result = await download_and_stage_eras()
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR: {e}")
        stage_result = {"_exception": str(e)}
    print(f"  result: {stage_result}")

    print("\nStep 2: post_pending_eras() ...")
    try:
        post_result = await post_pending_eras()
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR: {e}")
        post_result = {"errors": 1, "failed_files": [], "_exception": str(e)}
    print(f"  posted={post_result.get('posted', 0)} "
          f"errors={post_result.get('errors', 0)} "
          f"skipped_unpostable={post_result.get('skipped_unpostable', 0)}")

    errors = post_result.get("errors", 0)
    if errors > 0 or post_result.get("_exception"):
        if not CLICKUP_TOKEN:
            print("\nStep 3: ClickUp token not set — cannot create failure task")
            return
        print(f"\nStep 3: creating ClickUp task for Desiree ({errors} failure(s))...")
        title, body = _build_task_body(stage_result, post_result)
        task_result = await _create_desiree_task(title, body)
        if not task_result.get("ok"):
            print(f"  ✗ Task creation failed: {task_result}")
            return
        task = task_result["task"]
        task_id = task.get("id", "")
        print(f"  ✓ Task created: {task.get('url', '')}")
        print(f"    ID: {task_id}")

        print(f"\nStep 3b: attaching 835 files to task {task_id}...")
        for f in post_result.get("failed_files", []):
            file_name = f.get("file_name", "")
            era_id = _parse_era_id_from_filename(file_name)
            if not era_id:
                print(f"  ✗ can't parse era_id from {file_name!r}")
                continue
            clean_name = file_name.split(" - ")[0].strip()
            if not clean_name.endswith(".x12"):
                clean_name += ".x12"
            await _attach_835_to_task(task_id, era_id, clean_name)
    else:
        print("\nStep 3: no errors — no ClickUp task needed.")

    print("\nDone.")


if __name__ == "__main__":
    asyncio.run(main())
