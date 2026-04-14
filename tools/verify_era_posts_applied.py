"""Audit whether a set of ERA file_vals reported as 'posted' actually landed
in Lauris AR Information by looking them up via Check_Number (TRN02).

Use after any `post_pending_eras()` run to catch silent failures — if an ERA
appears as 'posted' in data/posted_eras.json but its Check_Number isn't in the
AR Information XML view, it didn't actually apply to AR and needs follow-up.

Input file format: JSON array of
    [{"file_val": "...", "file_name": "era_<payer>_<eraid>.x12", "expected_amount": 123.45}, ...]

Usage:
    # audit a specific set (from a JSON file):
    python3 tools/verify_era_posts_applied.py path/to/input.json

    # audit the most recent N entries from posted_eras.json (all tonight's work):
    python3 tools/verify_era_posts_applied.py --recent 26
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")

from sources.claimmd_api import ClaimMDAPI  # noqa: E402

AR_INFO_URL = (
    "https://www12.laurisonline.com/reports/formsearchdataviewXML.aspx"
    "?viewid=fwR8FpcbZLiOYvlDtnhz8A%3d%3d"
)


def _parse_era_id_from_filename(fname: str) -> str:
    m = re.search(r"era[_-](?:[A-Z0-9]+_)?(\d+)", fname)
    return m.group(1) if m else ""


def _parse_trn02(content: str) -> str:
    for seg in content.split("~"):
        parts = seg.strip().split("*")
        if parts and parts[0] == "TRN" and len(parts) >= 3:
            return parts[2].strip()
    return ""


def _fetch_ar_check_numbers() -> dict:
    """Returns {check_number: {count, total}}."""
    r = requests.get(
        AR_INFO_URL,
        auth=(os.environ["LAURIS_USERNAME"], os.environ["LAURIS_PASSWORD"]),
        timeout=600,
    )
    r.raise_for_status()
    root = ET.fromstring(r.text)
    checks = {}
    for row in root.findall("AR_Information_View"):
        ck = (row.findtext("Check_Number") or "").strip()
        if not ck:
            continue
        try:
            a = float(row.findtext("Received_Amount") or 0)
        except ValueError:
            a = 0.0
        slot = checks.setdefault(ck, {"count": 0, "total": 0.0})
        slot["count"] += 1
        slot["total"] += a
    return checks


async def main():
    ap = argparse.ArgumentParser(description="Verify ERA posts actually applied to Lauris AR")
    ap.add_argument("input", nargs="?", help="Path to JSON file with {file_val, file_name, expected_amount} entries")
    ap.add_argument("--recent", type=int, help="Audit the N most recent entries from data/posted_eras.json")
    args = ap.parse_args()

    entries = []
    if args.input:
        entries = json.loads(Path(args.input).read_text())
    elif args.recent:
        posted = json.loads(Path("data/posted_eras.json").read_text())
        # posted_eras.json is sorted alphanumerically so tail ≠ most recent
        # chronologically. For audit, caller should pass an explicit input file.
        print(f"⚠️ --recent uses alphanumeric tail of posted_eras.json, not insertion order")
        recent_vals = sorted(posted)[-args.recent:]
        entries = [{"file_val": v, "file_name": "", "expected_amount": 0.0} for v in recent_vals]
    else:
        ap.error("Provide either an input JSON file or --recent N")

    print(f"Auditing {len(entries)} entries...")

    api = ClaimMDAPI()
    print("Downloading 835s from Claim.MD to extract TRN02...")
    resolved = []
    for e in entries:
        fname = e.get("file_name", "")
        era_id = _parse_era_id_from_filename(fname) if fname else ""
        trn02 = ""
        if era_id:
            try:
                content = await api.download_era_835(era_id)
                if content:
                    trn02 = _parse_trn02(content)
            except Exception as exc:  # noqa: BLE001
                print(f"  ✗ {fname}: download failed ({exc})")
        resolved.append({**e, "era_id": era_id, "trn02": trn02})

    print(f"\nFetching Lauris AR Information XML (~50MB, can take a minute)...")
    checks = _fetch_ar_check_numbers()
    print(f"  {len(checks)} unique check numbers in AR Info view")

    print("\nResults:")
    print(f"{'file_val':<12} {'era_id':<10} {'TRN02':<20} {'Expected':>12} {'Hits':>6}  ok")
    applied = 0
    missing = []
    for e in resolved:
        hits = checks.get(e["trn02"], {"count": 0, "total": 0.0})
        ok = hits["count"] > 0 if e["trn02"] else False
        mark = "✓" if ok else ("✗" if e["trn02"] else "?")
        print(f"{e['file_val']:<12} {e['era_id']:<10} {e['trn02']:<20} "
              f"${e.get('expected_amount', 0):>10,.2f} {hits['count']:>6}  {mark}")
        if ok:
            applied += 1
        elif e["trn02"]:
            missing.append(e)

    print(f"\n{applied}/{len(resolved)} entries verified in AR Info view")
    if missing:
        print(f"\nMissing from AR (possibly silent failures — investigate):")
        for e in missing:
            print(f"  {e['file_val']}: {e['file_name']} TRN={e['trn02']}")


if __name__ == "__main__":
    asyncio.run(main())
