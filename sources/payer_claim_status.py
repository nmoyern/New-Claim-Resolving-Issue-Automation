"""
sources/payer_claim_status.py
------------------------------
Unified claim status check across payers.

Availity 276 for Anthem, Aetna, Molina, Humana.
Optum SearchClaim GraphQL for UHC.

Returns a standardized result: paid / denied / pending / not_found / error.
"""
from __future__ import annotations

import os
import time
import uuid as _uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urlencode

import aiohttp

from config.entities import BillingEntity
from config.models import Claim, MCO
from logging_utils.logger import get_logger
from sources.payer_inquiry import AVAILITY_PAYER_IDS, OPTUM_PAYER_ID

logger = get_logger("payer_claim_status")


@dataclass
class PayerClaimStatusResult:
    status: str  # "paid", "denied", "pending", "not_found", "error"
    paid_amount: float = 0.0
    check_number: str = ""
    effective_date: str = ""
    denial_codes: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict)


class PayerClaimStatusChecker:
    """Check current claim status with the payer."""

    def __init__(self):
        self._availity = _AvailityClaimStatus()
        self._optum = _OptumClaimStatus()

    async def check_status(
        self, claim: Claim, entity: BillingEntity,
    ) -> PayerClaimStatusResult:
        if claim.mco == MCO.UNITED:
            return await self._optum.check(claim, entity)
        if claim.mco in AVAILITY_PAYER_IDS:
            return await self._availity.check(claim, entity)
        return PayerClaimStatusResult(
            status="error",
            denial_codes=[f"No claim status API for {claim.mco.value}"],
        )


# ======================================================================
# Availity 276 Claim Status
# ======================================================================

class _AvailityClaimStatus:

    def __init__(self):
        self.client_id = os.getenv("AVAILITY_PROD_CLIENT_ID", "")
        self.client_secret = os.getenv("AVAILITY_PROD_CLIENT_SECRET", "")
        self.base_url = os.getenv(
            "AVAILITY_BASE_URL", "https://api.availity.com",
        )

    async def check(
        self, claim: Claim, entity: BillingEntity,
    ) -> PayerClaimStatusResult:
        if not (self.client_id and self.client_secret):
            return PayerClaimStatusResult(
                status="error",
                denial_codes=["Availity credentials not configured"],
            )

        payer_id = AVAILITY_PAYER_IDS.get(claim.mco)
        if not payer_id:
            return PayerClaimStatusResult(
                status="error",
                denial_codes=[f"No Availity payer ID for {claim.mco.value}"],
            )

        # Resolve payer-specific member ID for Anthem
        member_id = claim.client_id
        if claim.mco == MCO.ANTHEM:
            member_id = await self._resolve_anthem_member(claim, entity)
            if not member_id:
                return PayerClaimStatusResult(
                    status="error",
                    denial_codes=["Could not resolve Anthem member ID"],
                )

        from sources.payer_auth_lookup import _claim_name_parts, _claim_dob
        first, last = _claim_name_parts(claim)
        dob = _claim_dob(claim)

        token = await self._token()
        payload = {
            "payer.id": payer_id,
            "submitter.lastName": "LIFECONSULTANTS",
            "submitter.id": entity.availity_submitter_id,
            "providers.lastName": entity.availity_provider_name,
            "providers.npi": entity.billing_npi,
            "subscriber.memberId": member_id,
            "fromDate": claim.dos.strftime("%Y-%m-%d") if claim.dos else "",
            "toDate": claim.dos.strftime("%Y-%m-%d") if claim.dos else "",
        }
        if first:
            payload["subscriber.firstName"] = first
            payload["patient.firstName"] = first
        if last:
            payload["subscriber.lastName"] = last
            payload["patient.lastName"] = last
        if dob:
            payload["patient.birthDate"] = dob
        gender = getattr(claim, "gender_code", "")
        if gender in ("M", "F"):
            payload["patient.genderCode"] = gender
        payload["patient.subscriberRelationshipCode"] = "18"

        raw = await self._submit_and_poll(token, payload)
        return self._parse(raw)

    async def _resolve_anthem_member(
        self, claim: Claim, entity: BillingEntity,
    ) -> str:
        """Resolve Anthem member ID via Claim.MD 270 eligibility."""
        from sources.payer_auth_lookup import (
            _claim_name_parts, _claim_dob, ELIG_PAYER_IDS,
        )
        elig_payer = ELIG_PAYER_IDS.get(MCO.ANTHEM, "00180")
        first, last = _claim_name_parts(claim)
        dob = _claim_dob(claim)
        if not (first and last and dob):
            return ""
        try:
            from sources.claimmd_api import ClaimMDAPI
            api = ClaimMDAPI()
            result = await api.check_eligibility(
                member_last=last, member_first=first,
                payer_id=elig_payer,
                service_date=(
                    claim.dos.strftime("%Y%m%d") if claim.dos else ""
                ),
                provider_npi=entity.billing_npi,
                provider_taxid=entity.tax_id,
                member_id=claim.client_id,
                member_dob=dob.replace("-", ""),
            )
            return (result.get("elig", {}).get("ins_number", "") or "").strip()
        except Exception as exc:
            logger.warning("Anthem member resolve failed", error=str(exc)[:100])
            return ""

    async def _token(self) -> str:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/availity/v1/token",
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data=urlencode({
                    "grant_type": "client_credentials",
                    "scope": "healthcare-hipaa-transactions",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                }),
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise RuntimeError(f"Token failed: {data}")
                return data["access_token"]

    async def _submit_and_poll(
        self, token: str, payload: dict, max_wait: int = 45,
    ) -> dict:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-HTTP-Method-Override": "GET",
            "Accept": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/availity/v1/claim-statuses",
                headers=headers,
                data=urlencode(payload),
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                body = await resp.json(content_type=None)
                if resp.status not in (200, 202):
                    return {"_error": body}
                href = (
                    (body.get("claimStatuses") or [{}])[0]
                    .get("links", {})
                    .get("self", {})
                    .get("href")
                )
                if not href:
                    return {"_error": "No polling link in response"}

            import asyncio
            deadline = time.time() + max_wait
            while time.time() < deadline:
                await asyncio.sleep(2)
                async with session.get(
                    href,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Accept": "application/json",
                    },
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as poll:
                    if poll.status == 202:
                        continue
                    return await poll.json(content_type=None)

        return {"_error": "276 poll timed out"}

    def _parse(self, raw: dict) -> PayerClaimStatusResult:
        if raw.get("_error"):
            return PayerClaimStatusResult(
                status="error", raw=raw,
                denial_codes=[str(raw["_error"])[:200]],
            )

        details: list[dict] = []
        for cs in raw.get("claimStatuses", []):
            for d in cs.get("statusDetails", []):
                details.append(d)

        if not details:
            return PayerClaimStatusResult(status="not_found", raw=raw)

        paid_amount = 0.0
        check_number = ""
        effective_date = ""
        has_paid = False
        has_denied = False
        has_pending = False
        denial_codes: list[str] = []

        for d in details:
            cat = d.get("categoryCode", "")
            status_code = d.get("statusCode", "")
            amt = d.get("paidAmount")
            chk = d.get("checkNumber", "")
            eff = d.get("effectiveDate", "")

            if amt:
                try:
                    paid_amount = max(paid_amount, float(amt))
                except (ValueError, TypeError):
                    pass

            if cat == "F1" or status_code == "65" or paid_amount > 0:
                has_paid = True
                if chk:
                    check_number = chk
                if eff:
                    effective_date = eff

            if cat in ("F3", "F0") and not paid_amount:
                has_denied = True

            if cat.startswith("P") or cat.startswith("A"):
                has_pending = True

            if cat == "D0":
                pass  # data search unsuccessful

        if has_paid:
            return PayerClaimStatusResult(
                status="paid",
                paid_amount=paid_amount,
                check_number=check_number,
                effective_date=effective_date,
                raw=raw,
            )
        if has_denied:
            return PayerClaimStatusResult(
                status="denied",
                denial_codes=denial_codes,
                raw=raw,
            )
        if has_pending:
            return PayerClaimStatusResult(status="pending", raw=raw)

        return PayerClaimStatusResult(status="not_found", raw=raw)


# ======================================================================
# Optum SearchClaim
# ======================================================================

class _OptumClaimStatus:

    QUERY = """query SearchClaim($searchClaimInput: SearchClaimInput!) {
  searchClaim(searchClaimInput: $searchClaimInput) {
    claims {
      claimNumber claimStatus hasClaimDetails
      member { firstName lastName dateOfBirth memberId subscriberId }
      claimEvents { serviceStartDate serviceEndDate processedDate }
      claimLevelInfo { patientAccountNumber claimType }
      claimLevelTotalAmount {
        totalBilledChargeAmount totalPaidAmount totalAllowedAmount
      }
      claimAdjudicationCodes { claimCodeType code description }
      payments {
        paymentNumber paymentAmount paymentIssueDate
      }
    }
    pagination { hasMoreRecords }
  }
}"""

    def __init__(self):
        self.client_id = os.getenv("OPTUM_CLIENT_ID", "")
        self.client_secret = os.getenv("OPTUM_CLIENT_SECRET", "")
        self.token_url = os.getenv(
            "OPTUM_TOKEN_URL",
            "https://sandbox-apigw.optum.com/apip/auth/sntl/v1/token",
        )
        self.base_url = os.getenv(
            "OPTUM_BASE_URL",
            "https://sandbox-apigw.optum.com/oihub/claim/inquiry/v1",
        )

    async def check(
        self, claim: Claim, entity: BillingEntity,
    ) -> PayerClaimStatusResult:
        if not (self.client_id and self.client_secret):
            return PayerClaimStatusResult(
                status="error",
                denial_codes=["Optum credentials not configured"],
            )

        token = await self._token()
        pcn = str(getattr(claim, "patient_account_number", "") or "").strip()

        # Search by PCN first
        raw = None
        if pcn:
            raw = await self._search(
                token, entity,
                {"patientAccountNumber": pcn, "payerId": OPTUM_PAYER_ID},
            )

        claims = (raw or {}).get("claims", [])

        # Fall back to member + DOS
        if not claims:
            from sources.payer_auth_lookup import _claim_dob
            dob = _claim_dob(claim)
            dos = claim.dos.strftime("%m/%d/%Y") if claim.dos else ""
            if claim.client_id and dob and dos:
                dob_fmt = dob.replace("-", "/") if "-" in dob else dob
                raw = await self._search(
                    token, entity,
                    {
                        "memberId": claim.client_id,
                        "memberDateOfBirth": dob_fmt,
                        "serviceStartDate": dos,
                        "serviceEndDate": dos,
                        "payerId": OPTUM_PAYER_ID,
                    },
                )
                claims = (raw or {}).get("claims", [])

        if not claims:
            return PayerClaimStatusResult(status="not_found", raw=raw or {})

        return self._parse(claims[0])

    async def _token(self) -> str:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.token_url,
                data={
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "grant_type": "client_credentials",
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise RuntimeError(f"Optum token failed: {data}")
                return data["access_token"]

    async def _search(
        self, token: str, entity: BillingEntity, search_input: dict,
    ) -> dict:
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "providerTaxId": entity.tax_id,
            "x-optum-consumer-correlation-id": str(_uuid.uuid4()),
            # NO 'environment' header → live data
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.base_url,
                headers=headers,
                json={
                    "query": self.QUERY,
                    "variables": {
                        "searchClaimInput": search_input,
                        "operationName": "searchClaim",
                    },
                },
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                body = await resp.json(content_type=None)
                return (body.get("data", {}).get("searchClaim", {}) or {})

    def _parse(self, claim_data: dict) -> PayerClaimStatusResult:
        status = claim_data.get("claimStatus", "")
        totals = claim_data.get("claimLevelTotalAmount", {})
        try:
            paid = float(
                (totals.get("totalPaidAmount", "0") or "0")
                .replace(",", "").replace("$", "")
            )
        except (ValueError, TypeError):
            paid = 0.0

        # Extract denial/adjudication codes
        adj_codes = claim_data.get("claimAdjudicationCodes", [])
        denial_codes = [
            f"{c.get('code', '')}: {c.get('description', '')}"
            for c in adj_codes if c.get("code")
        ]

        # Payment info
        check_number = ""
        payments = claim_data.get("payments", []) or []
        if payments:
            check_number = payments[0].get("paymentNumber", "")

        effective = (
            claim_data.get("claimEvents", {}).get("processedDate", "")
        )

        if status == "Finalized" and paid > 0:
            return PayerClaimStatusResult(
                status="paid",
                paid_amount=paid,
                check_number=check_number,
                effective_date=effective,
                raw=claim_data,
            )

        if status == "Denied":
            return PayerClaimStatusResult(
                status="denied",
                denial_codes=denial_codes,
                raw=claim_data,
            )

        if status in ("Acknowledgement", "In Process", "Pending"):
            return PayerClaimStatusResult(
                status="pending", raw=claim_data,
            )

        return PayerClaimStatusResult(
            status="denied" if denial_codes else "not_found",
            denial_codes=denial_codes,
            raw=claim_data,
        )
