"""
Payer authorization/company lookup adapters.

These classes feed actions.company_auth_match.classify_company_auth_match().
They do not modify Claim.MD; they only answer whether a payer-side lookup
matches a claim under a specific billing entity.
"""
from __future__ import annotations

import os
import time
import uuid
from datetime import date
from typing import Any
from urllib.parse import urlencode

import aiohttp

from actions.company_auth_match import AuthLookupResult
from config.entities import BillingEntity
from config.models import Claim, MCO
from logging_utils.logger import get_logger
from sources.payer_inquiry import AVAILITY_PAYER_IDS, OPTUM_PAYER_ID, ensure_claim_patient_identity

logger = get_logger("payer_auth_lookup")


class PayerAuthorizationLookup:
    """Route authorization/company lookup to Optum for UHC, Availity otherwise."""

    def __init__(self):
        self.optum = OptumAuthorizationLookup()
        self.availity = AvailityEntityClaimStatusLookup()

    async def check_authorization(
        self,
        claim: Claim,
        entity: BillingEntity,
    ) -> AuthLookupResult:
        if claim.mco == MCO.UNITED:
            return await self.optum.check_authorization(claim, entity)
        return await self.availity.check_authorization(claim, entity)


class OptumAuthorizationLookup:
    """Optum/UHC prior-auth lookup using the auth/referral GraphQL endpoint."""

    search_prior_auths_query = """
query SearchPriorAuths($priorAuthSearchInput: PriorAuthSearchInput!) {
  searchPriorAuths(priorAuthSearchInput: $priorAuthSearchInput) {
    caseSummary {
      serviceReferenceNumber
      memberID
      memberFirstName
      memberLastName
      serviceDates
      caseStatus
      overallCoverageStatus
    }
    caseDescription {
      caseDetail {
        serviceReferenceNumber
        caseStatus
        expectedServiceStartDate
        expectedServiceEndDate
      }
      coverageStatus {
        procedureCode
        description
        coverageStatus
        decisionDate
      }
      patientDetails {
        id
        firstName
        lastName
        dateOfBirth
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
        self.auth_url = os.getenv(
            "OPTUM_AUTH_URL",
            "https://sandbox-apigw.optum.com/oihub/patient/auth/referral/v1",
        )
        self.environment = os.getenv("OPTUM_ENVIRONMENT", "sandbox")
        self._token = ""
        self._token_expiry = 0.0

    async def check_authorization(
        self,
        claim: Claim,
        entity: BillingEntity,
    ) -> AuthLookupResult:
        if not (self.client_id and self.client_secret):
            return AuthLookupResult(
                found=False,
                entity=entity,
                reason="Optum credentials are not configured.",
            )

        first, last = _claim_name_parts(claim)
        patient_dob = _claim_dob(claim)
        if not (first and last and claim.client_id and patient_dob):
            return AuthLookupResult(
                found=False,
                entity=entity,
                reason="Optum auth lookup needs member ID, first name, last name, and DOB.",
            )

        search_input = {
            "corporateTin": entity.tax_id,
            "provider": {
                "lastOrOrgName": entity.availity_provider_name,
            },
            "member": {
                "firstName": first,
                "lastName": last,
                "id": claim.client_id,
                "dateOfBirth": patient_dob,
            },
            "status": "All",
            "startDate": _ymd(claim.dos),
            "endDate": _ymd(claim.dos),
            "payerId": OPTUM_PAYER_ID,
        }
        raw = await self._graphql(
            "SearchPriorAuths",
            self.search_prior_auths_query,
            {"priorAuthSearchInput": search_input, "operationName": "SearchPriorAuths"},
            entity,
        )
        return _auth_result_from_optum(raw, entity)

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
        entity: BillingEntity,
    ) -> dict[str, Any]:
        token = await self._token_value()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "providerTaxId": entity.tax_id,
            "x-optum-consumer-correlation-id": str(uuid.uuid4()),
        }
        if self.environment:
            headers["environment"] = self.environment

        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.auth_url,
                headers=headers,
                json={"query": query, "variables": variables},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                body = await resp.json(content_type=None)
                return {"status_code": resp.status, "body": body, "operation": operation_name}


class AvailityEntityClaimStatusLookup:
    """
    Availity-backed entity sweep.

    Availity's claim-status response is used as the company match signal here:
    if a query under a specific entity returns payer records that are not D0
    "data search unsuccessful", that entity is treated as a match candidate.
    """

    def __init__(self):
        self.client_id = os.getenv("AVAILITY_PROD_CLIENT_ID", "")
        self.client_secret = os.getenv("AVAILITY_PROD_CLIENT_SECRET", "")
        self.base_url = os.getenv("AVAILITY_BASE_URL", "https://api.availity.com")

    async def check_authorization(
        self,
        claim: Claim,
        entity: BillingEntity,
    ) -> AuthLookupResult:
        await ensure_claim_patient_identity(claim)
        payer_id = AVAILITY_PAYER_IDS.get(claim.mco)
        if not payer_id:
            return AuthLookupResult(
                found=False,
                entity=entity,
                reason=f"Availity payer ID is not configured for {claim.mco.value}.",
            )
        if not (self.client_id and self.client_secret):
            return AuthLookupResult(
                found=False,
                entity=entity,
                reason="Availity credentials are not configured.",
            )

        token = await self._token_value()
        payload = {
            "payer.id": payer_id,
            "submitter.lastName": "LIFECONSULTANTS",
            "submitter.id": entity.availity_submitter_id,
            "providers.lastName": entity.availity_provider_name,
            "providers.npi": entity.billing_npi,
            "subscriber.memberId": claim.client_id,
            "fromDate": _ymd(claim.dos),
            "toDate": _ymd(claim.dos),
        }
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
        return _auth_result_from_availity(raw, entity)

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

    async def _submit_and_poll(
        self,
        token: str,
        payload: dict[str, str],
        max_wait: int = 45,
    ) -> dict[str, Any]:
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


def _auth_result_from_optum(raw: dict[str, Any], entity: BillingEntity) -> AuthLookupResult:
    if not raw or raw.get("status_code", 500) >= 400 or raw.get("body", {}).get("errors"):
        return AuthLookupResult(
            found=False,
            entity=entity,
            reason="Optum authorization lookup returned an error.",
            raw=raw or {},
        )

    data = raw.get("body", {}).get("data", {}).get("searchPriorAuths") or {}
    summaries = data.get("caseSummary") or []
    descriptions = data.get("caseDescription") or []
    if isinstance(summaries, dict):
        summaries = [summaries]
    if isinstance(descriptions, dict):
        descriptions = [descriptions]

    auth_number = ""
    if summaries:
        auth_number = str(summaries[0].get("serviceReferenceNumber", "") or "")
    if not auth_number and descriptions:
        detail = descriptions[0].get("caseDetail") or {}
        auth_number = str(detail.get("serviceReferenceNumber", "") or "")

    found = bool(summaries or descriptions)
    return AuthLookupResult(
        found=found,
        entity=entity,
        auth_number=auth_number,
        reason="Optum prior-auth record found." if found else "No Optum prior-auth record found.",
        raw=raw,
    )


def _auth_result_from_availity(raw: dict[str, Any], entity: BillingEntity) -> AuthLookupResult:
    if not raw or raw.get("_error") or raw.get("_http"):
        return AuthLookupResult(
            found=False,
            entity=entity,
            reason="Availity entity lookup returned an error.",
            raw=raw or {},
        )

    records = []
    for claim_status in raw.get("claimStatuses") or []:
        for detail in claim_status.get("statusDetails") or []:
            records.append({
                "categoryCode": detail.get("categoryCode", ""),
                "statusCode": detail.get("statusCode", ""),
            })

    categories = {r["categoryCode"] for r in records if r["categoryCode"]}
    found = bool(records) and categories != {"D0"}
    return AuthLookupResult(
        found=found,
        entity=entity,
        reason=(
            "Availity returned payer records under this entity."
            if found else "Availity returned no matching payer records for this entity."
        ),
        raw=raw,
    )


def _split_name(name: str) -> tuple[str, str]:
    parts = str(name or "").strip().split()
    if len(parts) < 2:
        return "", ""
    return parts[0], parts[-1]


def _claim_name_parts(claim: Claim) -> tuple[str, str]:
    first = str(getattr(claim, "patient_first_name", "") or "").strip()
    last = str(getattr(claim, "patient_last_name", "") or "").strip()
    if first and last:
        return first, last
    full_name = str(getattr(claim, "patient_full_name", "") or "").strip()
    if full_name:
        first, last = _split_name(full_name)
        if first and last:
            return first, last
    return _split_name(claim.client_name)


def _claim_dob(claim: Claim) -> str:
    for attr in ("client_dob", "patient_dob", "dob", "date_of_birth"):
        value = getattr(claim, attr, None)
        if isinstance(value, date):
            return value.strftime("%Y-%m-%d")
        if value:
            return str(value)
    return ""


def _ymd(value: date) -> str:
    return value.strftime("%Y-%m-%d")


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
