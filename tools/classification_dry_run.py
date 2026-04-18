#!/usr/bin/env python3
"""
Generate a classification-only dry-run report.

This tool is intentionally read-only:
  - no ERA posting
  - no Claim.MD changes
  - no Claim.MD response cursor movement
  - no ClickUp task creation
"""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
os.chdir(PROJECT_ROOT)
sys.path.insert(0, str(PROJECT_ROOT))

# Set before importing project modules that read config.settings.DRY_RUN.
os.environ["DRY_RUN"] = "true"
load_dotenv(PROJECT_ROOT / ".env")
os.environ["DRY_RUN"] = "true"

from reporting.classification_report import run_classification_report  # noqa: E402


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run the claim decision tree and write a dry-run report.",
    )
    parser.add_argument(
        "--max-claims",
        type=int,
        default=50,
        help="Maximum number of in-scope claims to include. Use 0 for no limit.",
    )
    parser.add_argument(
        "--full-pull",
        action="store_true",
        help="Read Claim.MD responses from the beginning without saving the cursor.",
    )
    parser.add_argument(
        "--no-payer-api",
        action="store_true",
        help="Skip Optum/Availity calls and only show local decision-tree context.",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()
    report = await run_classification_report(
        max_claims=args.max_claims,
        full_pull=args.full_pull,
        include_payer_api=not args.no_payer_api,
    )
    output = report["output"]
    counts = report["counts"]
    print("Classification dry run complete.")
    print(f"Included claims: {counts['included_in_report']}")
    print(f"Markdown report: {output['markdown_path']}")
    print(f"JSON report: {output['json_path']}")
    if output.get("markdown_uploaded_to_dropbox") or output.get("json_uploaded_to_dropbox"):
        print(f"Markdown Dropbox path: {output['markdown_dropbox_path']}")
        print(f"JSON Dropbox path: {output['json_dropbox_path']}")
    elif output.get("markdown_upload_error") or output.get("json_upload_error"):
        print("Dropbox API upload was not completed; report was saved locally.")
        print(output.get("markdown_upload_error") or output.get("json_upload_error"))


if __name__ == "__main__":
    asyncio.run(main())
