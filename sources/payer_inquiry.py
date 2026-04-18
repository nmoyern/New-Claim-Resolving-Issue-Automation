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
    MCO.UNITED: "87726",
    MCO.SENTARA: "54154",
    MCO.AETNA: "ABHVA",
    MCO.ANTHEM: "423",
    MCO.MOLINA: "MCC02",
    MCO.HUMANA: "61101",
    MCO.MAGELLAN: "38217",
    # DMAS: no valid Availity 276 payer ID found — routes to unsupported_mco
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


def _mco_to_claimmd_payer_id(mco: MCO) -> str:
    """Map MCO enum to the Claim.MD payer ID needed for eligibility lookups."""
    _MAP = {
        MCO.UNITED: OPTUM_PAYER_ID,
        **AVAILITY_PAYER_IDS,
    }
    return _MAP.get(mco, "")


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
    """Route the claim to the payer API requested for this new automation.

    For United claims, Optum is checked first. If Optum says 'paid' but the
    paid amount doesn't match the billed amount, Availity is checked as a
    second opinion — Availity matches on the exact PCN/claim control number
    and may reveal the specific line was actually denied.
    """
    if claim.mco == MCO.UNITED:
        optum_result = await OptumClaimInquiryClient().check_claim(claim)

        # Cross-check: if Optum says paid but amount doesn't match billed,
        # verify with Availity which matches on the exact PCN
        if (
            optum_result.bucket == "paid_at_payer"
            and claim.billed_amount > 0
            and abs(optum_result.paid_amount - claim.billed_amount) > 0.01
        ):
            logger.info(
                "Optum paid amount mismatch — cross-checking with Availity",
                claim_id=claim.claim_id,
                billed=claim.billed_amount,
                optum_paid=optum_result.paid_amount,
            )
            availity_result = await AvailityClaimStatusClient().check_claim(claim)
            if availity_result.ok and availity_result.bucket in (
                "real_denial", "payer_rejected", "payer_no_record",
            ):
                logger.info(
                    "Availity contradicts Optum — claim is denied",
                    claim_id=claim.claim_id,
                    optum_bucket=optum_result.bucket,
                    availity_bucket=availity_result.bucket,
                )
                availity_result.reason = (
                    f"{availity_result.reason} "
                    f"(Optum showed paid ${optum_result.paid_amount:,.2f} "
                    f"but billed was ${claim.billed_amount:,.2f} — "
                    f"Availity confirms this specific claim line was denied.)"
                )
                return availity_result

        return optum_result
    return await AvailityClaimStatusClient().check_claim(claim)


async def ensure_claim_patient_identity(claim: Claim) -> Claim:
    """
    Fill missing patient name, DOB, and gender from Claim.MD eligibility.

    Handles two cases:
    1. Lauris matched but DOB/Gender view was missing the patient
    2. Lauris didn't match at all — client_name is a fake PCN like CW4178-1181497

    In case 2, the eligibility call uses just member_id (no name required
    for most payers) and recovers the real patient name along with DOB/gender.
    """
    name_is_pcn = _looks_like_pcn(claim.client_name)
    has_dob = bool(_claim_dob(claim))
    first, last = _claim_name_parts(claim)
    has_name = bool(first and last)

    # Nothing to recover if we already have name + DOB
    if has_dob and has_name:
        return claim

    if not (claim.client_id and claim.mco != MCO.UNKNOWN):
        return claim

    try:
        from sources.claimmd_api import ClaimMDAPI

        api = ClaimMDAPI()
        if not api.key:
            return claim
        entity = get_entity_by_npi(claim.npi) or get_entity_by_program(claim.program)
        provider_npi = entity.billing_npi if entity else claim.npi
        provider_taxid = entity.tax_id if entity else ""
        elig_payer_id = getattr(claim, "claimmd_payer_id", "") or _mco_to_claimmd_payer_id(claim.mco)
        if not elig_payer_id:
            logger.warning("No Claim.MD payer ID for eligibility fallback", claim_id=claim.claim_id, mco=claim.mco.value)
            return claim

        # Build eligibility request — name is optional when member_id is provided
        elig_kwargs = {
            "payer_id": elig_payer_id,
            "service_date": claim.dos.strftime("%Y%m%d"),
            "provider_npi": provider_npi,
            "provider_taxid": provider_taxid,
            "member_id": claim.client_id,
            "member_last": last if has_name else "",
            "member_first": first if has_name else "",
        }
        elig = await api.check_eligibility(**elig_kwargs)

        # Response may be {"elig": {"ins_dob": ..., "ins_sex": ...}} or flat
        elig_data = elig.get("elig", elig) if isinstance(elig.get("elig"), dict) else elig

        # Recover patient name
        elig_first = str(elig_data.get("ins_name_f", "") or "").strip()
        elig_last = str(elig_data.get("ins_name_l", "") or "").strip()
        if elig_first and elig_last and (name_is_pcn or not has_name):
            claim.patient_first_name = elig_first.upper()
            claim.patient_last_name = elig_last.upper()
            claim.patient_full_name = f"{elig_last.upper()}, {elig_first.upper()}"
            claim.client_name = claim.patient_full_name
            logger.info(
                "Recovered patient name from Claim.MD eligibility",
                claim_id=claim.claim_id,
                name=claim.patient_full_name,
            )

        # Recover DOB
        dob = str(elig_data.get("ins_dob", "") or "").strip()
        if dob:
            if len(dob) == 8 and dob.isdigit():
                dob = f"{dob[:4]}-{dob[4:6]}-{dob[6:8]}"
            claim.client_dob = dob
            logger.info("Recovered patient DOB from Claim.MD eligibility", claim_id=claim.claim_id, dob=dob)

        # Recover gender
        gender_raw = str(elig_data.get("ins_sex", "") or "").strip()
        if gender_raw and not getattr(claim, "gender_code", ""):
            claim.gender_code = gender_raw[0].upper() if gender_raw else ""
    except Exception as exc:  # noqa: BLE001
        logger.warning("Claim.MD eligibility fallback failed", claim_id=claim.claim_id, error=str(exc))
    return claim


def _looks_like_pcn(value: str) -> bool:
    raw = str(value or "").strip().upper()
    return raw.startswith("CW") and "-" in raw


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
        result = self._classify(raw)

        # If the primary entity shows denial/no-record, check other LCI entities
        # — the payer may have processed the claim under a sibling entity's NPI.
        if result.bucket in ("real_denial", "payer_no_record", "payer_rejected"):
            alt_result = await self._check_sibling_entities(
                claim, token, payer_id, entity, first, last, dob, gender_code,
            )
            if alt_result:
                return alt_result

        return result

    async def _check_sibling_entities(
        self, claim, token, payer_id, primary_entity, first, last, dob, gender_code,
    ) -> PayerInquiryResult | None:
        """Try other LCI entities when the primary one shows denial."""
        from config.entities import get_all_entities

        for alt in get_all_entities():
            if alt is primary_entity:
                continue
            payload = {
                "payer.id": payer_id,
                "submitter.lastName": "LIFECONSULTANTS",
                "submitter.id": alt.availity_submitter_id,
                "providers.npi": alt.billing_npi,
                "providers.lastName": alt.availity_provider_name,
                "subscriber.memberId": claim.client_id,
                "fromDate": _ymd(claim.dos),
                "toDate": _ymd(claim.dos),
                "patient.subscriberRelationshipCode": "18",
            }
            if first:
                payload["subscriber.firstName"] = first
                payload["patient.firstName"] = first
            if last:
                payload["subscriber.lastName"] = last
                payload["patient.lastName"] = last
            if dob:
                payload["patient.birthDate"] = dob
            if gender_code:
                payload["patient.genderCode"] = gender_code

            raw = await self._submit_and_poll(token, payload)
            alt_result = self._classify(raw)
            if alt_result.bucket == "paid_at_payer":
                logger.info(
                    "Claim paid under sibling entity",
                    claim_id=claim.claim_id,
                    billed_entity=primary_entity.key if primary_entity else claim.npi,
                    paid_entity=alt.key,
                    paid_amount=alt_result.paid_amount,
                )
                alt_result.reason = (
                    f"{alt_result.reason} "
                    f"(Paid to {alt.display_name}, not {primary_entity.display_name if primary_entity else 'unknown'}.)"
                )
                return alt_result
        return None

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
