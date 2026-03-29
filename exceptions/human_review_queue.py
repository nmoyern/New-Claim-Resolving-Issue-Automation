"""
exceptions/human_review_queue.py
---------------------------------
Manages the queue of claims that require human intervention.
Writes to a local JSON file + sends summary via ClickUp comment.
"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import List

from config.models import ResolutionResult
from config.settings import LOG_DIR
from logging_utils.logger import get_logger

QUEUE_FILE = LOG_DIR / f"human_review_{date.today().isoformat()}.json"
logger = get_logger("human_review")


class HumanReviewQueue:
    def __init__(self):
        self._queue: List[dict] = []
        QUEUE_FILE.parent.mkdir(parents=True, exist_ok=True)

    def add(self, result: ResolutionResult):
        self._queue.append({
            "claim_id":    result.claim.claim_id,
            "client_name": result.claim.client_name,
            "mco":         result.claim.mco.value,
            "dos":         str(result.claim.dos),
            "action":      result.action_taken.value,
            "reason":      result.human_reason,
            "timestamp":   result.timestamp.isoformat(),
        })
        logger.warning(
            "Claim flagged for human review",
            claim_id=result.claim.claim_id,
            reason=result.human_reason[:80],
        )

    def save(self):
        with open(QUEUE_FILE, "w") as f:
            json.dump(self._queue, f, indent=2)
        logger.info("Human review queue saved", count=len(self._queue), path=str(QUEUE_FILE))

    def to_summary_text(self) -> str:
        if not self._queue:
            return ""
        lines = [f"🔍 Human Review Queue — {date.today().strftime('%m/%d/%y')} ({len(self._queue)} items):"]
        for item in self._queue[:10]:  # Cap at 10 to keep comment readable
            lines.append(
                f"  • Claim {item['claim_id']} ({item['client_name']}, {item['mco']}): {item['reason'][:80]}"
            )
        if len(self._queue) > 10:
            lines.append(f"  ... and {len(self._queue) - 10} more. See {QUEUE_FILE.name}")
        return "\n".join(lines)

    @property
    def count(self) -> int:
        return len(self._queue)
