"""
logging_utils/logger.py
-----------------------
Structured JSON logging + ClickUp daily comment posting.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Optional

import aiohttp
import structlog

from config.settings import (
    CLICKUP_API_TOKEN,
    CLICKUP_DAILY_TASK_ID,
    LOG_DIR,
    LOG_LEVEL,
    DRY_RUN,
)


# ---------------------------------------------------------------------------
# Setup structured logging
# ---------------------------------------------------------------------------

def setup_logging():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_file = LOG_DIR / f"claims_{date.today().isoformat()}.jsonl"

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

    # Also write to file
    file_handler = logging.FileHandler(log_file)
    file_handler.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))

    root = logging.getLogger()
    root.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
    root.addHandler(file_handler)
    root.addHandler(stream_handler)


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)


# ---------------------------------------------------------------------------
# ClickUp daily comment poster
# ---------------------------------------------------------------------------

class ClickUpLogger:
    """Posts daily run summary as a comment on the Claims Daily Tasks ClickUp task."""

    BASE_URL = "https://api.clickup.com/api/v2"

    def __init__(self, task_id: str = CLICKUP_DAILY_TASK_ID):
        self.task_id = task_id
        self.token = CLICKUP_API_TOKEN
        self.logger = get_logger("clickup_logger")

    async def post_comment(self, text: str) -> bool:
        if DRY_RUN:
            self.logger.info("DRY_RUN: Would post ClickUp comment", text=text[:100])
            return True

        if not self.token:
            self.logger.warning("No CLICKUP_API_TOKEN set — skipping ClickUp comment")
            return False

        url = f"{self.BASE_URL}/task/{self.task_id}/comment"
        headers = {
            "Authorization": self.token,
            "Content-Type": "application/json",
        }
        payload = {"comment_text": text, "notify_all": False}

        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status in (200, 201):
                    self.logger.info("ClickUp comment posted", task_id=self.task_id)
                    return True
                else:
                    body = await resp.text()
                    self.logger.error(
                        "ClickUp comment failed",
                        status=resp.status,
                        body=body[:200],
                    )
                    return False

    async def post_human_review_alert(self, claim_id: str, reason: str):
        text = (
            f"⚠️ HUMAN REVIEW NEEDED — Claim {claim_id}: {reason} "
            f"#AUTO #{date.today().strftime('%m/%d/%y')}"
        )
        await self.post_comment(text)


# ---------------------------------------------------------------------------
# Google Sheets logger (Claim Denial Calls sheet)
# ---------------------------------------------------------------------------

class SheetsLogger:
    """Appends claim action rows to the Claim Denial Calls tracking sheet."""

    def __init__(self):
        self.logger = get_logger("sheets_logger")
        self._client = None

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import gspread
            from google.oauth2.service_account import Credentials
            from config.settings import CLAIM_DENIAL_CALLS_SHEET_ID, get_credentials

            sa_json = get_credentials().google_sa
            if not sa_json:
                return None
            scopes = [
                "https://spreadsheets.google.com/feeds",
                "https://www.googleapis.com/auth/drive",
            ]
            creds = Credentials.from_service_account_info(sa_json, scopes=scopes)
            self._client = gspread.authorize(creds)
        except Exception as e:
            self.logger.warning("Sheets client init failed", error=str(e))
        return self._client

    def log_claim_action(
        self,
        claim_id: str,
        client_name: str,
        mco: str,
        action: str,
        notes: str,
        follow_up_date: Optional[date] = None,
    ):
        if DRY_RUN:
            self.logger.info("DRY_RUN: Would log to Sheets", claim_id=claim_id, action=action)
            return

        gc = self._get_client()
        if not gc:
            return

        try:
            from config.settings import CLAIM_DENIAL_CALLS_SHEET_ID
            sheet = gc.open_by_key(CLAIM_DENIAL_CALLS_SHEET_ID)
            ws = sheet.sheet1
            row = [
                date.today().isoformat(),
                claim_id,
                client_name,
                mco,
                action,
                notes,
                follow_up_date.isoformat() if follow_up_date else "",
                "AUTO",
            ]
            ws.append_row(row)
            self.logger.info("Logged to Sheets", claim_id=claim_id)
        except Exception as e:
            self.logger.error("Sheets logging failed", error=str(e), claim_id=claim_id)
