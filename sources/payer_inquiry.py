"""
API-only payer claim status checks.

The watered-down workflow uses this module as the only payer-facing layer:
United Healthcare claims go to Optum, and every other MCO goes to Availity.
No fax systems, portal scraping, or document hunting happen here.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from urllib.parse import urlencode

import aiohttp

from config.entities import get_entity_by_npi, get_entity_by_program
from config.models import Claim, ClaimStatus, MCO
from logging_utils.logger import get_logger

logger = get_logger("payer_inquiry")


OPTUM_PAYER_ID = "87726"

AVAILITY_PAYER_IDS = {
    MCO.SENTARA: "54154",
    MCO.AETNA: "ABHVA",
    MCO.ANTHEM: "00423",
    MCO.MOLINA: "MCCVA",
    MCO.HUMANA: "61101",
    MCO.MAGELLAN: "38217",
    MCO.DMAS: "SPAYORCODE",
}


@dataclass
class PayerInquiryResult:
    """Small, plain result used by the orchestrator before routing a claim."""

    gateway: str
    bucket: str
    ok: bool
    should_process: bool
    reason: str
    paid_amount: float = 0.0
    detail_summary: str = ""
    detail_items: list[str] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


def is_billed_rejected_or_denied(claim: Claim) -> bool:
    """
    Keep only claims that were actually billed and came back rejected/denied.

    In Claim.MD data, a billed claim has a positive billed amount. A claim is
    actionable here only if Claim.MD says it was rejected or denied.
    """
    return (
        claim.billed_amount > 0
        and claim.status in {ClaimStatus.REJECTED, ClaimStatus.DENIED}
    )


async def check_payer_claim_status(claim: Claim) -> PayerInquiryResult:
    """Route the claim to the payer API requested for this new automation."""
    if claim.mco == MCO.UNITED:
        return await OptumClaimInquiryClient().check_claim(claim)
    return await AvailityClaimStatusClient().check_claim(claim)


async def ensure_claim_patient_identity(claim: Claim) -> Claim:
    """
    Fill missing patient DOB from Claim.MD eligibility when Lauris demographics
    did not provide it but we do have a usable patient name and member ID.
    """
    if _claim_dob(claim):
        return claim
    first, last = _claim_name_parts(claim)
    if not (first and last and claim.client_id and claim.mco != MCO.UNKNOWN):
        return claim

    try:
        from sources.claimmd_api import ClaimMDAPI

        api = ClaimMDAPI()
        if not api.key:
            return claim
        entity = get_entity_by_npi(claim.npi) or get_entity_by_program(claim.program)
        provider_npi = entity.billing_npi if entity else claim.npi
        provider_taxid = entity.tax_id if entity else ""
        elig = await api.check_eligibility(
            member_last=last,
            member_first=first,
            payer_id=claim.mco.value,
            service_date=claim.dos.strftime("%Y%m%d"),
            provider_npi=provider_npi,
            provider_taxid=provider_taxid,
            member_id=claim.client_id,
        )
        dob = str(elig.get("ins_dob", "") or "").strip()
        if dob:
            claim.client_dob = dob
            logger.info("Recovered patient DOB from Claim.MD eligibility", claim_id=claim.claim_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Claim.MD eligibility fallback failed", claim_id=claim.claim_id, error=str(exc))
    return claim


class OptumClaimInquiryClient:
    """Thin async client for Optum Real Claim Inquiry."""

    search_claim_query = """
query SearchClaim($searchClaimInput: SearchClaimInput!) {
  searchClaim(searchClaimInput: $searchClaimInput) {
    claims {
      claimNumber
      claimStatus
      hasClaimDetails
      claimEvents {
        receivedDate
        processedDate
        serviceStartDate
        serviceEndDate
      }
      claimLevelInfo {
        patientAccountNumber
        claimType
      }
      claimLevelTotalAmount {
        totalBilledChargeAmount
        totalPaidAmount
        totalAllowedAmount
      }
      claimStatusCrosswalkData {
        claim507Code
        claim507CodeDesc
        claim508Code
        claim508CodeDesc
      }
      claimAdjudicationCodes {
        claimCodeType
        code
        description
      }
    }
  }
}
"""

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
        self.environment = os.getenv("OPTUM_ENVIRONMENT", "sandbox")
        self._token = ""
        self._token_expiry = 0.0

    async def check_claim(self, claim: Claim) -> PayerInquiryResult:
        entity = _claim_entity(claim)
        if not (self.client_id and self.client_secret and entity and entity.tax_id):
            return PayerInquiryResult(
                gateway="optum",
                bucket="api_not_configured",
                ok=False,
                should_process=True,
                reason="Optum credentials or billing tax ID are not configured; keeping United claim in work queue.",
                detail_summary="Optum setup is missing credentials or a matching billing tax ID.",
            )

        variables = {
            "searchClaimInput": {
                "payerId": OPTUM_PAYER_ID,
                "claimNumber": claim.claim_id,
            },
            "operationName": "SearchClaim",
        }
        raw = await self._graphql(
            "SearchClaim",
            self.search_claim_query,
            variables,
            entity.tax_id,
        )
        return self._classify(claim, raw)

    async def _token_value(self) -> str:
        if self._token and time.time() < self._token_expiry - 60:
            return self._token

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
                    raise RuntimeError(f"Optum token failed: HTTP {resp.status} {data}")
                self._token = data["access_token"]
                self._token_expiry = time.time() + int(data.get("expires_in", 3600))
                return self._token

    async def _graphql(
        self,
        operation_name: str,
        query: str,
        variables: dict[str, Any],
        provider_tax_id: str,
    ) -> dict[str, Any]:
        token = await self._token_value()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "providerTaxId": provider_tax_id,
            "x-optum-consumer-correlation-id": str(uuid.uuid4()),
        }
        if self.environment:
            headers["environment"] = self.environment

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.base_url,
                headers=headers,
                json={"query": query, "variables": variables},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                body = await resp.json(content_type=None)
                return {"status_code": resp.status, "body": body, "operation": operation_name}

    def _classify(self, claim: Claim, raw: dict[str, Any]) -> PayerInquiryResult:
        if raw.get("status_code", 500) >= 400 or raw.get("body", {}).get("errors"):
            return PayerInquiryResult(
                gateway="optum",
                bucket="api_error",
                ok=False,
                should_process=True,
                reason="Optum could not confirm status; keeping claim in work queue.",
                detail_summary="Optum returned an error instead of claim detail.",
                raw=raw,
            )

        claims = raw.get("body", {}).get("data", {}).get("searchClaim", {}).get("claims") or []
        if not claims:
            return PayerInquiryResult(
                gateway="optum",
                bucket="payer_no_record",
                ok=True,
                should_process=True,
                reason="Optum has no United claim record; treat as real follow-up.",
                detail_summary="Optum returned no matching claim record.",
                raw=raw,
            )

        paid_total = 0.0
        status_words = []
        detail_items = []
        for item in claims:
            totals = item.get("claimLevelTotalAmount") or {}
            paid_total += _money(totals.get("totalPaidAmount"))
            status_words.append(str(item.get("claimStatus", "")).lower())
            detail_items.extend(_optum_claim_details(item))

        detail_summary = _join_detail_items(detail_items)

        if paid_total > 0 or any("paid" in s for s in status_words):
            return PayerInquiryResult(
                gateway="optum",
                bucket="paid_at_payer",
                ok=True,
                should_process=False,
                reason="Optum shows United paid this claim; no denial work needed.",
                paid_amount=paid_total,
                detail_summary=detail_summary or "Optum shows the claim as paid.",
                detail_items=detail_items,
                raw=raw,
            )

        if any("den" in s or "reject" in s for s in status_words):
            bucket = "real_denial" if claim.status == ClaimStatus.DENIED else "payer_rejected"
        else:
            bucket = "needs_follow_up"
        return PayerInquiryResult(
            gateway="optum",
            bucket=bucket,
            ok=True,
            should_process=True,
            reason="Optum confirms this United claim still needs work.",
            paid_amount=paid_total,
            detail_summary=detail_summary or "Optum confirms the claim still needs work.",
            detail_items=detail_items,
            raw=raw,
        )


class AvailityClaimStatusClient:
    """Async Availity 276/277 claim status client."""

    def __init__(self):
        self.client_id = os.getenv("AVAILITY_PROD_CLIENT_ID", "")
        self.client_secret = os.getenv("AVAILITY_PROD_CLIENT_SECRET", "")
        self.base_url = os.getenv("AVAILITY_BASE_URL", "https://api.availity.com")

    async def check_claim(self, claim: Claim) -> PayerInquiryResult:
        await ensure_claim_patient_identity(claim)
        payer_id = AVAILITY_PAYER_IDS.get(claim.mco)
        if not payer_id:
            return PayerInquiryResult(
                gateway="availity",
                bucket="unsupported_mco",
                ok=False,
                should_process=True,
                reason=f"Availity payer ID is not configured for {claim.mco.value}.",
                detail_summary=f"Availity payer ID is missing for {claim.mco.value}.",
            )
        if not (self.client_id and self.client_secret):
            return PayerInquiryResult(
                gateway="availity",
                bucket="api_not_configured",
                ok=False,
                should_process=True,
                reason="Availity credentials are not configured; keeping claim in work queue.",
                detail_summary="Availity credentials missing.",
            )

        token = await self._token_value()
        entity = get_entity_by_npi(claim.npi) or get_entity_by_program(claim.program)
        submitter_id = entity.availity_submitter_id if entity else ""
        provider_npi = entity.billing_npi if entity else claim.npi
        provider_name = entity.availity_provider_name if entity else ""
        payload = {
            "payer.id": payer_id,
            "submitter.lastName": "LIFECONSULTANTS",
            "providers.npi": provider_npi,
            "subscriber.memberId": claim.client_id,
            "fromDate": _ymd(claim.dos),
            "toDate": _ymd(claim.dos),
        }
        if submitter_id:
            payload["submitter.id"] = submitter_id
        if provider_name:
            payload["providers.lastName"] = provider_name
        first, last = _claim_name_parts(claim)
        dob = _claim_dob(claim)
        if first:
            payload["subscriber.firstName"] = first
            payload["patient.firstName"] = first
        if last:
            payload["subscriber.lastName"] = last
            payload["patient.lastName"] = last
        if dob:
            payload["patient.birthDate"] = dob
        gender_code = getattr(claim, "gender_code", "")
        if gender_code:
            payload["patient.genderCode"] = gender_code
        payload["patient.subscriberRelationshipCode"] = "18"
        raw = await self._submit_and_poll(token, payload)
        return self._classify(raw)

    async def _token_value(self) -> str:
        body = urlencode({
            "grant_type": "client_credentials",
            "scope": "healthcare-hipaa-transactions",
            "client_id": self.client_id,
            "client_secret": self.client_secret,
        })
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self.base_url}/availity/v1/token",
                headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
                data=body,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise RuntimeError(f"Availity token failed: HTTP {resp.status} {data}")
                return data["access_token"]

    async def _submit_and_poll(self, token: str, payload: dict[str, str], max_wait: int = 45) -> dict[str, Any]:
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
                    return {"_http": resp.status, "_error": body}
                href = ((body.get("claimStatuses") or [{}])[0].get("links") or {}).get("self", {}).get("href")
                if not href:
                    return {"_http": resp.status, "_error": "Availity response did not include polling link."}

            deadline = time.time() + max_wait
            while time.time() < deadline:
                await _sleep(2)
                async with session.get(
                    href,
                    headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as poll_resp:
                    poll_body = await poll_resp.json(content_type=None)
                    if poll_resp.status == 200:
                        return poll_body
                    if poll_resp.status != 202:
                        return {"_http": poll_resp.status, "_error": poll_body}
        return {"_http": "timeout", "_error": "Availity claim-status poll timed out."}

    def _classify(self, raw: dict[str, Any]) -> PayerInquiryResult:
        if not raw or raw.get("_error") or raw.get("_http"):
            return PayerInquiryResult(
                gateway="availity",
                bucket="api_error",
                ok=False,
                should_process=True,
                reason="Availity could not confirm status; keeping claim in work queue.",
                detail_summary="Availity returned an error instead of claim detail.",
                raw=raw or {},
            )

        records = []
        paid_total = 0.0
        detail_items = []
        for claim_status in raw.get("claimStatuses") or []:
            for detail in claim_status.get("statusDetails") or []:
                paid = _money(detail.get("paymentAmount"))
                paid_total += paid
                records.append({
                    "categoryCode": detail.get("categoryCode", ""),
                    "paid": paid,
                    "claimAmount": detail.get("claimAmount", ""),
                })
                detail_items.append(_availity_detail_text(detail))

        categories = {r["categoryCode"] for r in records if r["categoryCode"]}
        if records and categories == {"D0"}:
            bucket = "payer_no_record"
            should_process = True
            reason = "Availity says the payer has no matching claim record."
        elif categories & {"A3", "A4", "A6", "A7"}:
            bucket = "payer_rejected"
            should_process = True
            reason = "Availity says the payer rejected the claim at intake."
        elif categories == {"A1"} and paid_total == 0:
            bucket = "too_new"
            should_process = False
            reason = "Availity says the payer received it but has not decided yet."
        elif paid_total > 0 or "F1" in categories:
            bucket = "paid_at_payer"
            should_process = False
            reason = "Availity says the payer paid this claim."
        elif "F2" in categories:
            bucket = "real_denial"
            should_process = True
            reason = "Availity confirms this is a real payer denial."
        else:
            bucket = "needs_follow_up"
            should_process = True
            reason = "Availity returned an unresolved status that needs follow-up."

        return PayerInquiryResult(
            gateway="availity",
            bucket=bucket,
            ok=True,
            should_process=should_process,
            reason=reason,
            paid_amount=paid_total,
            detail_summary=_join_detail_items(detail_items) or reason,
            detail_items=detail_items,
            raw=raw,
        )


def _money(value: Any) -> float:
    try:
        return float(str(value or "0").replace(",", "").replace("$", ""))
    except (TypeError, ValueError):
        return 0.0


def _ymd(value: date) -> str:
    return value.strftime("%Y-%m-%d")


def _claim_name_parts(claim: Claim) -> tuple[str, str]:
    first = str(getattr(claim, "patient_first_name", "") or "").strip()
    last = str(getattr(claim, "patient_last_name", "") or "").strip()
    if first and last:
        return first, last
    full_name = str(getattr(claim, "patient_full_name", "") or "").strip()
    if full_name:
        parts = full_name.split()
        if len(parts) >= 2:
            return parts[0], parts[-1]
    parts = str(claim.client_name or "").strip().split()
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return "", ""


def _claim_dob(claim: Claim) -> str:
    for attr in ("client_dob", "patient_dob", "dob", "date_of_birth"):
        value = getattr(claim, attr, None)
        if isinstance(value, date):
            return value.strftime("%Y-%m-%d")
        if value:
            return str(value)
    return ""


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


def attach_payer_api_details_to_claim(claim: Claim, result: PayerInquiryResult) -> Claim:
    """Attach normalized payer findings to the claim for downstream reports."""
    claim.payer_api_gateway = result.gateway
    claim.payer_api_bucket = result.bucket
    claim.payer_api_reason = result.reason
    claim.payer_api_detail_summary = result.detail_summary
    claim.payer_api_detail_items = list(result.detail_items)
    claim.payer_api_paid_amount = result.paid_amount
    return claim


def _optum_claim_details(item: dict[str, Any]) -> list[str]:
    details: list[str] = []
    claim_status = str(item.get("claimStatus", "")).strip()
    if claim_status:
        details.append(f"Claim status: {claim_status}")

    crosswalk_data = item.get("claimStatusCrosswalkData") or {}
    if isinstance(crosswalk_data, dict):
        crosswalk_items = [crosswalk_data]
    elif isinstance(crosswalk_data, list):
        crosswalk_items = [entry for entry in crosswalk_data if isinstance(entry, dict)]
    else:
        crosswalk_items = []

    for crosswalk in crosswalk_items:
        for key in ("claim507CodeDesc", "claim508CodeDesc"):
            value = str(crosswalk.get(key, "")).strip()
            if value:
                details.append(value)

    for code in item.get("claimAdjudicationCodes") or []:
        description = str(code.get("description", "")).strip()
        code_value = str(code.get("code", "")).strip()
        code_type = str(code.get("claimCodeType", "")).strip()
        parts = [part for part in (code_type, code_value, description) if part]
        if parts:
            details.append(" - ".join(parts))
    return _dedupe_keep_order(details)


def _availity_detail_text(detail: dict[str, Any]) -> str:
    category = str(detail.get("categoryCode", "")).strip()
    status = str(detail.get("statusCode", "")).strip()
    description = (
        str(detail.get("statusCodeDescription", "")).strip()
        or str(detail.get("industryCodeDescription", "")).strip()
        or str(detail.get("entityDescription", "")).strip()
    )
    parts = [part for part in (category, status, description) if part]
    return " - ".join(parts)


def _join_detail_items(items: list[str]) -> str:
    unique = _dedupe_keep_order([item for item in items if item])
    return " | ".join(unique[:6])


def _claim_entity(claim: Claim):
    entity = get_entity_by_npi(getattr(claim, "npi", ""))
    if entity:
        return entity
    return get_entity_by_program(getattr(claim, "program", ""))


def _dedupe_keep_order(items: list[str]) -> list[str]:
    seen = set()
    out = []
    for item in items:
        key = item.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out
