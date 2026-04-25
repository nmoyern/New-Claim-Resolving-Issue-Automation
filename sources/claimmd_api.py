"""
sources/claimmd_api.py
----------------------
Claim.MD REST API client — replaces browser automation for core operations.

API docs: https://api.claim.md
Base URL: https://svc.claim.md/services/

This is faster, more reliable, and doesn't need browser sessions or CAPTCHA.
Uses AccountKey from .env (CLAIMMD_API_KEY).
"""
from __future__ import annotations

import os
from datetime import date, datetime
from typing import Dict, List, Optional, Tuple

import aiohttp

from config.entities import get_entity_by_npi
from config.models import Claim, ClaimStatus, DenialCode, MCO, Program
from config.settings import DRY_RUN
from sources.claimmd import parse_denial_codes, _parse_mco, _parse_date
from logging_utils.logger import get_logger

logger = get_logger("claimmd_api")

# Payer ID → MCO mapping (from Claim.MD payer list)
# Complete payer ID → MCO mapping (from Claim.MD payer list + claim data analysis)
PAYER_MCO_MAP = {
    # Sentara / Optima (9,218 claims — largest MCO)
    "54154": "sentara",    # Optima/Sentara Health Plan
    "VAPRM": "sentara",    # Sentara Virginia Premier alt ID
    "00453": "sentara",    # Sentara alt payer ID
    # Aetna (1,203 claims)
    "128VA": "aetna",      # Aetna Better Health of Virginia
    # Molina (849 claims)
    "MCCVA": "molina",     # Molina Complete Care of Virginia
    "MCC02": "molina",     # Molina Complete Care alt ID
    # United (118 claims)
    "87726": "united",     # United Health Care
    "77350": "united",     # UHC alt
    # Anthem (16 claims)
    "00423": "anthem",     # VA BCBS / Anthem Blue Cross
    "00923": "anthem",     # Anthem alt
    "SB923": "anthem",     # Anthem alt
    # DMAS / Straight Medicaid (66 claims)
    "SPAYORCODE": "dmas",  # Straight Medicaid / DMAS
    "VAMCD": "dmas",       # Virginia Medicaid / DMAS
    # Humana
    "31140": "humana",     # Humana
    "61101": "humana",     # Humana alt
    # Magellan
    "38217": "magellan",   # Magellan
}

API_BASE = "https://svc.claim.md/services"
API_KEY = os.getenv("CLAIMMD_API_KEY", "")

# Track last response ID between runs so we only fetch new updates
LAST_RESPONSE_FILE = os.path.join("data", "last_responseid.txt")

# Claims older than this are ignored/archived
MAX_CLAIM_AGE_DAYS = 365

# Accumulates $0 claims for weekly ClickUp notification to Justin
_zero_dollar_claims: list = []

# Accumulates suspected duplicate claims (same member+DOS+program, diff PCN)
_suspected_duplicates: list = []
_dos_program_seen: dict = {}  # dedup_key → first claim_id

# HCPCS/CPT → service code mapping for Virginia HCBS waivers
PROC_SERVICE_MAP = {
    # Mental Health Skill-Building (MHSS)
    "H0046": "MHSS",
    "H2014": "MHSS",
    # Residential Community Support (RCSU)
    "H0019": "RCSU",
    "H2015": "RCSU",
    "H2016": "RCSU",
    # Community Living
    "H2015HQ": "COMMUNITY_LIVING",
    # Crisis
    "H0036": "CRISIS",
    "H2011": "CRISIS",
    # Psychosocial Rehab
    "H2017": "PSR",
    "H2018": "PSR",
}


def _proc_to_service_code(proc_code: str) -> str:
    """Map a procedure/HCPCS code to a service type."""
    if not proc_code:
        return ""
    code = proc_code.strip().upper()
    return PROC_SERVICE_MAP.get(code, "")


def _get_key() -> str:
    """Get API key, reading from env at call time."""
    key = os.getenv("CLAIMMD_API_KEY", "")
    if not key:
        from dotenv import dotenv_values
        config = dotenv_values(".env")
        key = config.get("CLAIMMD_API_KEY", "")
    return key


class ClaimMDAPI:
    """Claim.MD REST API client for claims, ERAs, and notes."""

    def __init__(self):
        self.base = API_BASE
        self.key = _get_key()
        if not self.key:
            logger.warning("CLAIMMD_API_KEY not set — API calls will fail")

    # ------------------------------------------------------------------
    # Response ID tracking (incremental fetching)
    # ------------------------------------------------------------------

    @staticmethod
    def _load_last_response_id() -> str:
        """Load the last response ID from disk so we only fetch new updates."""
        try:
            if os.path.exists(LAST_RESPONSE_FILE):
                with open(LAST_RESPONSE_FILE) as f:
                    rid = f.read().strip()
                    if rid:
                        return rid
        except Exception:
            pass
        return "0"

    @staticmethod
    def _save_last_response_id(rid: str):
        """Save the last response ID for next run."""
        os.makedirs(os.path.dirname(LAST_RESPONSE_FILE), exist_ok=True)
        with open(LAST_RESPONSE_FILE, "w") as f:
            f.write(str(rid))

    async def _post(self, endpoint: str, data: dict = None, timeout: int = 120) -> dict:
        """Make a POST request to the Claim.MD API."""
        payload = {"AccountKey": self.key}
        if data:
            payload.update(data)

        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base}/{endpoint}/",
                data=payload,
                headers={"Accept": "application/json"},
                timeout=aiohttp.ClientTimeout(total=timeout),
            ) as resp:
                if resp.status == 200:
                    return await resp.json()
                else:
                    text = await resp.text()
                    logger.error(
                        "API request failed",
                        endpoint=endpoint,
                        status=resp.status,
                        response=text[:200],
                    )
                    return {}

    # ------------------------------------------------------------------
    # Claims / Responses
    # ------------------------------------------------------------------

    async def get_claim_responses(
        self,
        response_id: str = "0",
        claim_id: str = "",
        save_cursor: bool = True,
    ) -> List[dict]:
        """
        Get claim status updates (denials, rejections, acceptances).
        Start with response_id="0" to get all, then use last_responseid
        for incremental updates.
        """
        data = {"ResponseID": response_id}
        if claim_id:
            data["ClaimID"] = claim_id

        result = await self._post("response", data)
        claims = result.get("claim", [])
        last_id = result.get("last_responseid", "0")

        # Save last response ID so next run only fetches new updates
        if save_cursor and last_id and last_id != "0":
            self._save_last_response_id(last_id)

        logger.info(
            "Got claim responses",
            count=len(claims),
            last_responseid=last_id,
        )
        return claims

    async def get_denied_claims(
        self,
        full_pull: bool = False,
        save_cursor: bool = True,
    ) -> List[Claim]:
        """
        Pull denied/rejected claims and convert to Claim objects.

        By default uses incremental fetching (only new responses since last run).
        Set full_pull=True to get all responses from the beginning.

        Claims with DOS older than 1 year are automatically filtered out.
        """
        if full_pull:
            response_id = "0"
        else:
            response_id = self._load_last_response_id()
            logger.info("Fetching claims since response ID", response_id=response_id)

        raw_claims = await self.get_claim_responses(
            response_id=response_id,
            save_cursor=save_cursor,
        )

        # Save the last response ID for next run
        if raw_claims:
            # The API returns last_responseid in the response
            # We stored it during get_claim_responses
            pass  # saved in get_claim_responses

        cutoff_date = date.today() - __import__("datetime").timedelta(days=MAX_CLAIM_AGE_DAYS)
        seen_ids = set()
        claims = []
        skipped_old = 0
        skipped_accepted = 0
        skipped_duplicate = 0

        for raw in raw_claims:
            status_code = raw.get("status", "")
            if status_code not in ("R", "D", "4", "22"):
                skipped_accepted += 1
                continue

            try:
                claim = self._raw_to_claim(raw)
                if not claim:
                    continue

                # Filter out claims older than 1 year
                if claim.dos < cutoff_date:
                    skipped_old += 1
                    continue

                # Filter out $0 claims — flag as data quality issue
                if claim.billed_amount <= 0:
                    logger.warning(
                        "$0 claim detected — archiving",
                        claim_id=claim.claim_id,
                        client=claim.client_name,
                    )
                    # Queue for weekly Justin notification
                    _zero_dollar_claims.append({
                        "claim_id": claim.claim_id,
                        "client_name": claim.client_name,
                        "client_id": claim.client_id,
                        "lauris_id": claim.lauris_id,
                        "dos": str(claim.dos),
                        "mco": claim.mco.value,
                        "program": claim.program.value,
                    })
                    continue

                # Deduplicate — only process each claim ID once
                if claim.claim_id in seen_ids:
                    skipped_duplicate += 1
                    continue
                seen_ids.add(claim.claim_id)

                # Check for suspected duplicates: same member + DOS + program
                # but different claim IDs (different PCN/LCN)
                dedup_key = (
                    f"{claim.client_id}|{claim.dos}|{claim.program.value}"
                )
                if dedup_key in _dos_program_seen:
                    existing_id = _dos_program_seen[dedup_key]
                    _suspected_duplicates.append({
                        "claim_id": claim.claim_id,
                        "existing_claim_id": existing_id,
                        "client_name": claim.client_name,
                        "client_id": claim.client_id,
                        "lauris_id": claim.lauris_id,
                        "dos": str(claim.dos),
                        "program": claim.program.value,
                        "amount": claim.billed_amount,
                    })
                    logger.info(
                        "Suspected duplicate claim — same member+DOS+program",
                        new_id=claim.claim_id,
                        existing_id=existing_id,
                        client=claim.client_name,
                    )
                    # Archive the duplicate and skip processing
                    continue
                _dos_program_seen[dedup_key] = claim.claim_id

                claims.append(claim)
            except Exception as e:
                logger.warning("Failed to parse claim", error=str(e))

        logger.info(
            f"Found {len(claims)} actionable denied/rejected claims",
            total_responses=len(raw_claims),
            skipped_accepted=skipped_accepted,
            skipped_old=skipped_old,
            skipped_duplicate=skipped_duplicate,
        )
        return claims

    def _raw_to_claim(self, raw: dict) -> Optional[Claim]:
        """Convert a raw API response dict to a Claim object."""
        claim_id = raw.get("claimmd_id", "")
        if not claim_id:
            return None

        # Parse dates
        fdos = _parse_date(raw.get("fdos", "")) or date.today()

        # Parse MCO from payer ID using our mapping first, then fallback
        payer_id = raw.get("payerid", "")
        mco_str = PAYER_MCO_MAP.get(payer_id, "")
        if mco_str:
            mco = _parse_mco(mco_str)
        else:
            payer_name = raw.get("payer_name", payer_id)
            mco = _parse_mco(payer_name)

        # Parse denial info from messages array
        # Each message has: status (R=reject, W=warning, A=accepted), message, mesgid, fields
        # Read ALL denial and warning messages — not just the first one
        messages = raw.get("messages", [])
        rejection_messages = []
        warning_messages = []
        for msg in messages:
            if isinstance(msg, dict):
                msg_status = msg.get("status", "")
                msg_text = msg.get("message", "")
                if msg_text:
                    if msg_status == "R":
                        rejection_messages.append(msg_text)
                    elif msg_status == "W":
                        warning_messages.append(msg_text)

        # Combine all denial + warning messages — denials first, then warnings
        all_denial_messages = rejection_messages + warning_messages
        denial_raw = " | ".join(all_denial_messages) if all_denial_messages else ""
        denial_codes = parse_denial_codes(denial_raw) if denial_raw else [DenialCode.UNKNOWN]

        # Store raw denial message in claim_history database
        if denial_raw:
            try:
                from reporting.gap_report import GapReporter
                reporter = GapReporter()
                reporter.store_raw_denial(claim_id, denial_raw)
                reporter.close()
            except Exception:
                pass  # Don't fail claim parsing for logging

        # Log unrecognized patterns to new_patterns table
        if denial_codes == [DenialCode.UNKNOWN] and denial_raw:
            try:
                from reporting.gap_report import GapReporter
                reporter = GapReporter()
                reporter.log_new_pattern(claim_id, denial_raw)
                reporter.close()
            except Exception:
                pass

        status_code = raw.get("status", "")
        status = ClaimStatus.REJECTED if status_code == "R" else ClaimStatus.DENIED

        try:
            billed = float(str(raw.get("total_charge", "0")).replace(",", "").replace("$", "") or "0")
        except (ValueError, TypeError):
            billed = 0.0
        bill_npi = raw.get("bill_npi", "")
        ins_number = raw.get("ins_number", "")
        patient_name = raw.get("pat_name", raw.get("ins_name", ""))
        pcn = raw.get("pcn", "")

        # Extract procedure code and units from service lines
        proc_code = raw.get("proc_code", "")
        units = 0.0
        try:
            units = float(raw.get("units", "0") or "0")
        except (ValueError, TypeError):
            units = 0.0

        # If no top-level proc_code, check service lines array
        service_lines = raw.get("service_lines", raw.get("lines", []))
        if not proc_code and isinstance(service_lines, list) and service_lines:
            first_line = service_lines[0] if service_lines else {}
            if isinstance(first_line, dict):
                proc_code = first_line.get("proc_code", first_line.get("cpt", ""))
                if not units:
                    try:
                        units = float(first_line.get("units", "0") or "0")
                    except (ValueError, TypeError):
                        pass

        # Infer service_code from proc_code
        service_code = _proc_to_service_code(proc_code)

        age_days = (date.today() - fdos).days if fdos else 0

        # Infer program from billing NPI
        entity = get_entity_by_npi(bill_npi)
        program = entity.program if entity else Program.UNKNOWN

        claim = Claim(
            claim_id=claim_id,
            client_name=patient_name if patient_name else pcn,
            client_id=ins_number,
            dos=fdos,
            mco=mco,
            program=program,
            billed_amount=billed,
            status=status,
            denial_codes=denial_codes,
            denial_reason_raw=denial_raw,
            date_denied=date.today(),
            age_days=age_days,
            npi=bill_npi,
            service_code=service_code,
            proc_code=proc_code,
            units=units,
            claimmd_url=f"https://www.claim.md/monitor.plx?l=claim&id={claim_id}",
        )
        claim.patient_account_number = pcn
        claim.claimmd_payer_id = payer_id
        return claim

    # ------------------------------------------------------------------
    # ERA operations
    # ------------------------------------------------------------------

    async def get_era_list(
        self,
        new_only: bool = False,
        received_date: str = "",
        payer_id: str = "",
        npi: str = "",
    ) -> List[dict]:
        """Get list of available ERAs."""
        data = {}
        if new_only:
            data["NewOnly"] = "1"
        if received_date:
            data["ReceivedDate"] = received_date
        if payer_id:
            data["PayerID"] = payer_id
        if npi:
            data["NPI"] = npi

        result = await self._post("eralist", data)
        eras = result.get("era", [])
        logger.info(f"Found {len(eras)} ERAs via API")
        return eras

    async def download_era_835(self, era_id: str, save_path: str = "") -> str:
        """Download ERA in 835 format. Optionally saves to file. Returns the raw content."""
        result = await self._post("era835", {"eraid": era_id})
        # API returns {"eraid": ..., "data": ["ISA*00*..."]}
        data = result.get("data", [])
        content = data[0] if isinstance(data, list) and data else str(data) if data else ""
        if content and save_path:
            import os
            os.makedirs(os.path.dirname(save_path), exist_ok=True)
            with open(save_path, "w") as f:
                f.write(content)
            logger.info("ERA 835 saved", era_id=era_id, path=save_path)
        return content

    async def download_era_data(self, era_id: str) -> dict:
        """Download ERA in structured JSON format."""
        return await self._post("eradata", {"eraid": era_id})

    # ------------------------------------------------------------------
    # Claim modification (corrections)
    # ------------------------------------------------------------------

    async def modify_claim(self, claim_id: str, corrections: dict) -> bool:
        """
        Modify/correct a claim via the API.
        corrections: dict of field names to new values.
        Supported fields: bill_npi, bill_taxid, ins_number (member ID),
                         pat_dob, diag_1, prov_npi, etc.
        """
        if DRY_RUN:
            logger.info("DRY_RUN: Would modify claim", claim_id=claim_id,
                        corrections=list(corrections.keys()))
            return True

        data = {"ClaimMD_ID": claim_id}

        # Map our field names to API field names
        field_map = {
            "member_id": "ins_number",
            "npi": "bill_npi",
            "rendering_npi": "prov_npi",
            "dob": "pat_dob",
            "diag": "diag_1",
            "billing_region": "bill_name",
            "auth_number": "prior_auth",
            "total_charge": "total_charge",
        }

        for field, value in corrections.items():
            api_field = field_map.get(field, field)
            data[api_field] = value

        result = await self._post("modify", data)
        success = bool(result)
        if success:
            logger.info("Claim modified via API", claim_id=claim_id,
                        fields=list(corrections.keys()))
        else:
            logger.warning("Claim modification failed", claim_id=claim_id)
        return success

    # ------------------------------------------------------------------
    # Claim notes
    # ------------------------------------------------------------------

    async def get_claim_notes(self, claim_id: str) -> List[dict]:
        """Get notes for a specific claim."""
        result = await self._post("notes", {"ClaimMD_ID": claim_id})
        return result.get("notes", [])

    async def add_claim_note(
        self, claim_id: str, note_text: str, pcn: str = "",
    ) -> bool:
        """Add a note to a claim via browser (clicks 'Add Note / Reminder').

        The Claim.MD notes API is read-only — notes can only be written
        through the web interface. This uses browser automation to search
        by PCN, open the claim, write the note, and click Add Note/Reminder.
        """
        if DRY_RUN:
            logger.info("DRY_RUN: Would add note", claim_id=claim_id, note=note_text[:60])
            return True

        if not pcn:
            logger.warning("PCN required for browser note posting", claim_id=claim_id)
            return False

        try:
            from sources.claimmd import post_claim_note
            success = await post_claim_note(claim_id, note_text, pcn=pcn)
            if success:
                logger.info("Note saved via browser", claim_id=claim_id, pcn=pcn)
            else:
                logger.warning("Note save failed via browser", claim_id=claim_id)
            return success
        except Exception as exc:
            logger.warning(
                "Browser note failed — note not saved",
                claim_id=claim_id,
                error=str(exc)[:100],
            )
            return False

    # ------------------------------------------------------------------
    # Claim appeal
    # ------------------------------------------------------------------

    async def submit_appeal(
        self, claim_id: str, appeal_data: dict | None = None,
    ) -> str:
        """Generate a Claim.MD appeal form URL via the API.

        Returns the appeal form URL on success, empty string on error.
        The URL loads an online form where the appeal can be completed
        and submitted (electronically, fax, mail, or download).

        Field is lowercase `claimid` — the Claim.MD internal claim ID
        (what we've been calling ClaimMD_ID elsewhere). The endpoint
        also accepts `remote_claimid` (REF*D9 from original submission)
        as an alternative.

        Optional contact fields (contact_name, contact_email, etc.) can
        be passed via appeal_data to pre-populate the appeal form.
        """
        if DRY_RUN:
            logger.info("DRY_RUN: Would submit appeal", claim_id=claim_id)
            return "dry-run-appeal-url"

        data = {"claimid": claim_id}
        if appeal_data:
            data.update(appeal_data)
        result = await self._post("appeal", data)

        if isinstance(result, dict) and result.get("error"):
            err = result["error"]
            logger.warning(
                "Appeal API error",
                claim_id=claim_id,
                error_code=err.get("error_code"),
                error_mesg=err.get("error_mesg", "")[:200],
            )
            return ""

        # Success response: {"link": [{"url": "..."}], "success": 1}
        links = result.get("link", []) if isinstance(result, dict) else []
        if isinstance(links, dict):
            links = [links]
        if links and isinstance(links[0], dict):
            url = links[0].get("url", "")
            if url:
                logger.info(
                    "Appeal form URL generated",
                    claim_id=claim_id,
                    url=url,
                )
                return url

        logger.warning("Appeal response missing URL", claim_id=claim_id)
        return ""

    # ------------------------------------------------------------------
    # Attach supporting documentation
    # ------------------------------------------------------------------

    async def upload_attachment(
        self, claim_id: str, file_path: str, filename: str = "",
    ) -> bool:
        """Upload a supporting document (PDF) to a claim.

        Uses the Claim.MD upload endpoint with multipart form data.
        The file is linked to the claim via ClaimMD_ID.
        """
        from pathlib import Path

        path = Path(file_path)
        if not path.exists():
            logger.warning("Attachment file not found", path=file_path)
            return False

        if DRY_RUN:
            logger.info(
                "DRY_RUN: Would upload attachment",
                claim_id=claim_id,
                file=file_path,
            )
            return True

        fname = filename or path.name
        file_data = path.read_bytes()

        try:
            import aiohttp

            form = aiohttp.FormData()
            form.add_field("AccountKey", self.key)
            form.add_field("ClaimMD_ID", claim_id)
            form.add_field(
                "File", file_data,
                filename=fname,
                content_type="application/pdf",
            )

            async with aiohttp.ClientSession() as session:
                async with session.post(
                    f"{self.base}upload/",
                    data=form,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as resp:
                    text = await resp.text()
                    success = "attachments pending" in text.lower() or resp.status == 200
                    if success:
                        logger.info(
                            "Attachment uploaded",
                            claim_id=claim_id,
                            file=fname,
                        )
                    else:
                        logger.warning(
                            "Attachment upload may have failed",
                            claim_id=claim_id,
                            response=text[:200],
                        )
                    return success
        except Exception as exc:
            logger.error(
                "Attachment upload failed",
                claim_id=claim_id,
                error=str(exc)[:200],
            )
            return False

    # ------------------------------------------------------------------
    # Archive old claims
    # ------------------------------------------------------------------

    async def archive_claim(self, claim_id: str) -> bool:
        """Archive a claim (for claims older than 1 year)."""
        if DRY_RUN:
            logger.info("DRY_RUN: Would archive claim", claim_id=claim_id)
            return True
        result = await self._post("archive", {"ClaimMD_ID": claim_id})
        success = bool(result)
        if success:
            logger.info("Claim archived via API", claim_id=claim_id)
        return success

    # ------------------------------------------------------------------
    # Payer list
    # ------------------------------------------------------------------

    async def get_payer_list(self) -> List[dict]:
        """Get list of available payers."""
        result = await self._post("payerlist")
        return result.get("payer", [])

    # ------------------------------------------------------------------
    # Eligibility verification
    # ------------------------------------------------------------------

    async def check_eligibility(
        self,
        member_last: str,
        member_first: str,
        payer_id: str,
        service_date: str,
        provider_npi: str,
        provider_taxid: str,
        member_id: str = "",
        member_dob: str = "",
    ) -> dict:
        """
        Real-time eligibility check via Claim.MD 270/271.
        Used for 'Coverage Terminated' denials per Claims Troubleshooting Guide.
        Returns eligibility response with coverage status.
        """
        data = {
            "ins_name_l": member_last,
            "ins_name_f": member_first,
            "payerid": payer_id,
            "pat_rel": "18",  # Self
            "fdos": service_date,  # yyyymmdd
            "prov_npi": provider_npi,
            "prov_taxid": provider_taxid,
        }
        if member_id:
            data["ins_number"] = member_id
        if member_dob:
            data["ins_dob"] = member_dob

        result = await self._post("eligdata", data)
        logger.info(
            "Eligibility check",
            member=f"{member_first} {member_last}",
            payer=payer_id,
            result="active" if result.get("active") else "inactive",
        )
        return result

    # ------------------------------------------------------------------
    # Webhook setup (real-time notifications)
    # ------------------------------------------------------------------

    async def setup_webhook(self, callback_url: str) -> bool:
        """
        Set up a webhook to receive real-time notifications from Claim.MD.
        Events: claim status updates, ERA receipts, etc.
        callback_url: URL that Claim.MD will POST to when events occur.
        """
        result = await self._post("Webhook", {
            "url": callback_url,
            "events": "response,era",
        })
        success = bool(result) and "error" not in str(result).lower()
        if success:
            logger.info("Webhook configured", url=callback_url)
        else:
            logger.warning("Webhook setup failed", result=str(result)[:200])
        return success

    # ------------------------------------------------------------------
    # Upload list
    # ------------------------------------------------------------------

    async def get_upload_list(self, upload_date: str = "") -> List[dict]:
        """Get list of previously uploaded files."""
        data = {}
        if upload_date:
            data["UploadDate"] = upload_date
        result = await self._post("uploadlist", data)
        return result.get("file", [])

    async def archive_claim(self, claim_id: str, reason: str = "") -> bool:
        """Archive a claim (e.g. $0 data quality issues)."""
        if DRY_RUN:
            logger.info("DRY_RUN: Would archive claim",
                        claim_id=claim_id, reason=reason)
            return True
        note = f"Archived: {reason}. #AUTO #{date.today().strftime('%m/%d/%y')}"
        await self.add_claim_note(claim_id, note)
        return True


async def flush_zero_dollar_claims():
    """
    Send weekly ClickUp notification to Justin about $0 claims.
    Called from orchestrator on Fridays or when queue has items.
    """
    global _zero_dollar_claims
    if not _zero_dollar_claims:
        return

    from actions.clickup_tasks import (
        ClickUpTaskCreator, _next_business_day, PRIORITY_NORMAL,
        get_assignees,
    )

    today_str = date.today().strftime("%m/%d/%y")
    count = len(_zero_dollar_claims)

    rows = []
    for c in _zero_dollar_claims:
        lauris = c.get('lauris_id', '')
        lid = f" | Lauris: {lauris}" if lauris else ""
        rows.append(
            f"  - {c['claim_id']} | {c['client_name']}"
            f"{lid} | Member: {c['client_id']} | "
            f"DOS: {c['dos']} | {c['mco']} / {c['program']}"
        )

    tc = ClickUpTaskCreator()
    await tc.create_task(
        list_id=tc.list_id,
        name=f"$0 Claims Data Quality — {count} claims [{today_str}]",
        description=(
            f"{count} claim(s) with $0 billed amount detected.\n"
            f"These have been archived as data quality issues.\n\n"
            f"Claims:\n" + "\n".join(rows) + "\n\n"
            f"Please investigate the source of these $0 claims.\n\n"
            f"Generated by Claims Automation on {today_str}."
        ),
        assignees=get_assignees("justin"),
        due_date=_next_business_day(),
        priority=PRIORITY_NORMAL,
    )

    logger.info("$0 claims ClickUp created for Justin", count=count)

    # Archive each claim in Claim.MD
    api = ClaimMDAPI()
    if api.key:
        for c in _zero_dollar_claims:
            await api.archive_claim(
                c["claim_id"], "$0 billed amount — data quality issue"
            )

    _zero_dollar_claims = []


async def flush_suspected_duplicates():
    """
    Send weekly ClickUp notification to Justin about suspected
    duplicate claims (same member + DOS + program, different PCN/LCN).
    """
    global _suspected_duplicates, _dos_program_seen
    if not _suspected_duplicates:
        _dos_program_seen = {}
        return

    from actions.clickup_tasks import (
        ClickUpTaskCreator, _next_business_day, PRIORITY_NORMAL,
        get_assignees,
    )

    today_str = date.today().strftime("%m/%d/%y")
    count = len(_suspected_duplicates)

    rows = []
    for d in _suspected_duplicates:
        lauris = d.get('lauris_id', '')
        lid = f" | Lauris: {lauris}" if lauris else ""
        rows.append(
            f"  - {d['client_name']}{lid} | Member: {d['client_id']}\n"
            f"    DOS: {d['dos']} | Program: {d['program']}\n"
            f"    Claim A: {d['existing_claim_id']}\n"
            f"    Claim B: {d['claim_id']} (${d['amount']:,.2f})\n"
        )

    tc = ClickUpTaskCreator()
    await tc.create_task(
        list_id=tc.list_id,
        name=(
            f"Suspected Duplicate Claims — {count} "
            f"[{today_str}]"
        ),
        description=(
            f"{count} suspected duplicate claim(s) detected.\n"
            f"Same member + DOS + program but different claim IDs.\n\n"
            + "\n".join(rows) + "\n"
            f"Please review and archive/skip as appropriate.\n\n"
            f"Generated by Claims Automation on {today_str}."
        ),
        assignees=get_assignees("justin"),
        due_date=_next_business_day(),
        priority=PRIORITY_NORMAL,
    )

    logger.info("Duplicate claims ClickUp created for Justin",
                count=count)
    _suspected_duplicates = []
    _dos_program_seen = {}
