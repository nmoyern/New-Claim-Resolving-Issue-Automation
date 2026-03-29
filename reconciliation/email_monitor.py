"""
reconciliation/email_monitor.py
--------------------------------
Monitors ea@lifeconsultantsinc.org for payment reconciliation commands.

Email types handled:
  1. Payment confirmation: Subject contains unique_code + "_PAID_DATE"
     → Marks payment as received in tracker
  2. Write-off request: Subject "underpayment" with Excel attachment
     → Processes write-offs from PCN list
  3. CANCEL: Reply with "CANCEL" in body → reverses the action

Security rules:
  - Only accept emails from @lifeconsultantsinc.org
  - Must CC nm@lifeconsultantsinc.org
  - CANCEL reverses actions
"""
from __future__ import annotations

import imaplib
import email
import os
import re
from datetime import date, datetime
from email.header import decode_header
from pathlib import Path
from typing import List, Optional, Tuple

from logging_utils.logger import get_logger

logger = get_logger("email_monitor")

IMAP_SERVER = "imap.gmail.com"
IMAP_PORT = 993
EMAIL_ADDRESS = os.getenv("AUTOMATION_EMAIL", "ea@lifeconsultantsinc.org")
EMAIL_PASSWORD = os.getenv("AUTOMATION_EMAIL_PASSWORD", "")
ALLOWED_DOMAIN = os.getenv("ALLOWED_DOMAIN", "lifeconsultantsinc.org")
REQUIRED_CC = os.getenv("REQUIRED_CC", "nm@lifeconsultantsinc.org")


class EmailMonitor:
    """Monitors email for payment reconciliation commands."""

    def __init__(self):
        self.email = EMAIL_ADDRESS
        self.password = EMAIL_PASSWORD
        self.mail: Optional[imaplib.IMAP4_SSL] = None

    def connect(self) -> bool:
        """Connect to Gmail via IMAP."""
        if not self.password:
            logger.warning("No email password configured")
            return False
        try:
            self.mail = imaplib.IMAP4_SSL(IMAP_SERVER, IMAP_PORT)
            self.mail.login(self.email, self.password)
            self.mail.select("INBOX")
            logger.info("Connected to email", address=self.email)
            return True
        except Exception as e:
            logger.error("Email connection failed", error=str(e))
            return False

    def check_for_commands(self) -> List[dict]:
        """Check inbox for unread payment reconciliation commands.
        Returns list of command dicts."""
        if not self.mail:
            if not self.connect():
                return []

        commands = []
        try:
            # Search for unread emails
            status, messages = self.mail.search(None, "UNSEEN")
            if status != "OK":
                return []

            for msg_id in messages[0].split():
                status, msg_data = self.mail.fetch(msg_id, "(RFC822)")
                if status != "OK":
                    continue

                msg = email.message_from_bytes(msg_data[0][1])
                cmd = self._parse_email(msg, msg_id.decode())
                if cmd:
                    commands.append(cmd)

        except Exception as e:
            logger.error("Email check failed", error=str(e))

        return commands

    def _parse_email(self, msg, msg_id: str) -> Optional[dict]:
        """Parse an email into a command dict if it passes security checks."""
        sender = self._decode_header(msg.get("From", ""))
        to = self._decode_header(msg.get("To", ""))
        cc = self._decode_header(msg.get("Cc", ""))
        subject = self._decode_header(msg.get("Subject", ""))
        body = self._get_body(msg)

        # Security check 1: sender must be from allowed domain
        sender_email = self._extract_email(sender)
        if not sender_email or not sender_email.endswith(f"@{ALLOWED_DOMAIN}"):
            logger.warning("Email rejected: invalid sender domain",
                           sender=sender_email)
            return None

        # Security check 2: must CC nm@lifeconsultantsinc.org
        cc_emails = [self._extract_email(addr) for addr in cc.split(",")]
        if REQUIRED_CC not in cc_emails:
            logger.warning("Email rejected: missing required CC",
                           sender=sender_email, cc=cc)
            return None

        # Check for CANCEL
        if "CANCEL" in body.upper():
            return {
                "type": "cancel",
                "email_id": msg_id,
                "sender": sender_email,
                "subject": subject,
                "body": body,
            }

        # Check for payment confirmation: code_PAID_DATE
        paid_match = re.search(r"(PAY-[A-Z0-9]{6})_PAID_(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{2,4})", subject)
        if paid_match:
            return {
                "type": "paid",
                "email_id": msg_id,
                "sender": sender_email,
                "unique_code": paid_match.group(1),
                "paid_date": paid_match.group(2),
                "subject": subject,
            }

        # Check for write-off/underpayment with Excel attachment
        if "underpayment" in subject.lower():
            attachments = self._get_attachments(msg)
            excel_files = [a for a in attachments
                           if a["filename"].endswith((".xlsx", ".xls", ".csv"))]
            if excel_files:
                return {
                    "type": "writeoff",
                    "email_id": msg_id,
                    "sender": sender_email,
                    "subject": subject,
                    "attachments": excel_files,
                }

        # Check for PCN write-off list (generic attachment)
        attachments = self._get_attachments(msg)
        excel_files = [a for a in attachments
                       if a["filename"].endswith((".xlsx", ".xls", ".csv"))]
        if excel_files and "write" in subject.lower():
            return {
                "type": "writeoff",
                "email_id": msg_id,
                "sender": sender_email,
                "subject": subject,
                "attachments": excel_files,
            }

        return None

    def _decode_header(self, header: str) -> str:
        """Decode email header."""
        if not header:
            return ""
        parts = decode_header(header)
        decoded = []
        for part, charset in parts:
            if isinstance(part, bytes):
                decoded.append(part.decode(charset or "utf-8", errors="replace"))
            else:
                decoded.append(str(part))
        return " ".join(decoded)

    def _extract_email(self, text: str) -> str:
        """Extract email address from header text."""
        match = re.search(r"[\w.+-]+@[\w.-]+\.\w+", text)
        return match.group(0).lower() if match else ""

    def _get_body(self, msg) -> str:
        """Extract plain text body from email."""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    payload = part.get_payload(decode=True)
                    if payload:
                        return payload.decode("utf-8", errors="replace")
        else:
            payload = msg.get_payload(decode=True)
            if payload:
                return payload.decode("utf-8", errors="replace")
        return ""

    def _get_attachments(self, msg) -> List[dict]:
        """Extract file attachments from email."""
        attachments = []
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_disposition() == "attachment":
                    filename = part.get_filename()
                    if filename:
                        data = part.get_payload(decode=True)
                        # Save to temp
                        save_dir = Path("/tmp/claims_work/email_attachments")
                        save_dir.mkdir(parents=True, exist_ok=True)
                        save_path = save_dir / filename
                        with open(save_path, "wb") as f:
                            f.write(data)
                        attachments.append({
                            "filename": filename,
                            "path": str(save_path),
                            "size": len(data),
                        })
        return attachments

    def disconnect(self):
        """Close IMAP connection."""
        if self.mail:
            try:
                self.mail.logout()
            except Exception:
                pass
            self.mail = None
