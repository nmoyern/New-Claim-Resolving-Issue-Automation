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

# Claim.MD eligibility payer IDs that return payer-specific member IDs.
# These may differ from the AVAILITY_PAYER_IDS used for 276/278 calls.
ELIG_PAYER_IDS = {
    MCO.ANTHEM: "00180",  # Anthem BCBS VA — returns Anthem member # (VAQ...)
    MCO.AETNA: "AETNA",
    MCO.HUMANA: "61101",
    MCO.MOLINA: "MCC02",
    MCO.SENTARA: "54154",
}

logger = get_logger("payer_auth_lookup")


class PayerAuthorizationLookup:
    """Route authorization/company lookup to Optum for UHC, Availity otherwise."""

    def __init__(self):
        self.optum = OptumAuthorizationLookup()
        self.optum_claims = OptumClaimInquiry()
        self.availity = AvailityEntityClaimStatusLookup()
        self.service_review = AvailityServiceReviewInquiry()

    async def check_authorization(
        self,
        claim: Claim,
        entity: BillingEntity,
    ) -> AuthLookupResult:
        if claim.mco == MCO.UNITED:
            return await self.optum.check_authorization(claim, entity)
        return await self.availity.check_authorization(claim, entity)

    async def obtain_authorization(
        self,
        claim: Claim,
        entity: BillingEntity,
    ) -> AuthLookupResult:
        """Obtain an authorization number from the payer.

        For non-UHC MCOs: Availity 278I Service Review Inquiry.
        For UHC: Optum SearchClaim (claim inquiry) to get claim status
        and denial details. The auth/referral SearchPriorAuths API does
        not support VA CCC+ Medicaid members in production.
        """
        if claim.mco == MCO.UNITED:
            return await self.optum_claims.lookup_claim(claim, entity)
        return await self.service_review.obtain_authorization(claim, entity)


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
        self.environment = os.getenv("OPTUM_ENVIRONMENT", "")
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
                "address": {
                    "addressLine1": entity.address_line1,
                    "city": entity.city,
                    "state": entity.state,
                    "zip": entity.zip_code,
                },
            },
            "member": {
                "firstName": first,
                "lastName": last,
                "id": claim.client_id,
                "dateOfBirth": patient_dob,
                "groupNumber": _optum_group_number(claim),
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


class OptumClaimInquiry:
    """Optum claim inquiry via SearchClaim GraphQL.

    Returns real claim data for UHC — status, denial codes, payments,
    and line-level adjudication.  Works for VA CCC+ Medicaid members
    (unlike SearchPriorAuths which only supports commercial plans).

    Key: omit the ``environment: sandbox`` header to get live data.
    """

    SEARCH_CLAIM_QUERY = """
query SearchClaim($searchClaimInput: SearchClaimInput!) {
  searchClaim(searchClaimInput: $searchClaimInput) {
    claims {
      claimNumber claimStatus hasClaimDetails
      member { firstName lastName dateOfBirth memberId subscriberId policyNumber }
      provider {
        submitted { billingTin billingProviderName billingNpi renderingProviderName }
        adjudicated { billingTin billingProviderName billingNpi }
      }
      claimEvents { receivedDate processedDate serviceStartDate serviceEndDate }
      claimLevelInfo { patientAccountNumber claimType }
      claimLevelTotalAmount {
        totalBilledChargeAmount totalPaidAmount totalAllowedAmount
      }
      claimAdjudicationCodes { claimCodeType code description }
      claimDetailedInformation {
        claimNumber adjudicatedClaimSummaryStatus
        diagnosisCodes { diagnosisCode diagnosisCodeType }
        lines {
          lineNumber procedureCode serviceCode modifiers unitCount
          lineEvents { serviceStartDate serviceEndDate }
          lineLevelTotalAmounts { billedChargeAmount paidAmount allowedAmount }
          lineAdjudicationCodes { type code description }
        }
      }
    }
    pagination { hasMoreRecords nextPageToken }
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
        self._token = ""
        self._token_expiry = 0.0

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

    async def search_by_pcn(
        self, pcn: str, entity: BillingEntity,
    ) -> dict[str, Any]:
        """Search Optum by Patient Account Number (PCN like CW4236-1201181)."""
        return await self._search(
            {"patientAccountNumber": pcn, "payerId": OPTUM_PAYER_ID},
            entity,
        )

    async def search_by_member(
        self,
        member_id: str,
        dob: str,
        dos_start: str,
        dos_end: str,
        entity: BillingEntity,
    ) -> dict[str, Any]:
        """Search Optum by member ID + DOB + service dates."""
        return await self._search(
            {
                "memberId": member_id,
                "memberDateOfBirth": dob,
                "serviceStartDate": dos_start,
                "serviceEndDate": dos_end,
                "payerId": OPTUM_PAYER_ID,
            },
            entity,
        )

    async def lookup_claim(
        self, claim: Claim, entity: BillingEntity,
    ) -> AuthLookupResult:
        """Look up a UHC claim and extract auth/denial info.

        Tries PCN first, then falls back to member ID + DOS.
        """
        if not (self.client_id and self.client_secret):
            return AuthLookupResult(
                found=False, entity=entity,
                reason="Optum claim inquiry credentials not configured.",
            )

        pcn = str(getattr(claim, "patient_account_number", "") or "").strip()
        raw = None

        # Try PCN first (most reliable match)
        if pcn:
            raw = await self.search_by_pcn(pcn, entity)

        # Fallback to member + DOS
        claims = (raw or {}).get("claims", [])
        if not claims:
            dob = _claim_dob(claim)
            if dob and claim.client_id:
                dos_str = claim.dos.strftime("%m/%d/%Y") if claim.dos else ""
                if dos_str:
                    raw = await self.search_by_member(
                        claim.client_id, dob.replace("-", "/") if "-" in dob
                        else dob, dos_str, dos_str, entity,
                    )
                    claims = (raw or {}).get("claims", [])

        if not claims:
            return AuthLookupResult(
                found=False, entity=entity,
                reason="Optum claim inquiry returned no claims.",
                raw=raw or {},
            )

        return _auth_result_from_optum_claim(claims[0], entity, claim)

    async def _search(
        self, search_input: dict, entity: BillingEntity,
    ) -> dict[str, Any]:
        token = await self._token_value()
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "providerTaxId": entity.tax_id,
            "x-optum-consumer-correlation-id": str(uuid.uuid4()),
            # NO 'environment' header = live data
        }
        variables: dict[str, Any] = {
            "searchClaimInput": search_input,
            "operationName": "searchClaim",
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(
                self.base_url,
                headers=headers,
                json={"query": self.SEARCH_CLAIM_QUERY, "variables": variables},
                timeout=aiohttp.ClientTimeout(total=120),
            ) as resp:
                body = await resp.json(content_type=None)
                data = body.get("data", {}).get("searchClaim", {}) or {}
                return data


def _auth_result_from_optum_claim(
    claim_data: dict, entity: BillingEntity, claim: Claim,
) -> AuthLookupResult:
    """Parse Optum SearchClaim result into AuthLookupResult."""
    status = claim_data.get("claimStatus", "")
    claim_num = claim_data.get("claimNumber", "")
    totals = claim_data.get("claimLevelTotalAmount", {})
    paid = float(totals.get("totalPaidAmount", "0") or "0")
    billed = float(totals.get("totalBilledChargeAmount", "0") or "0")

    # Extract denial/adjudication codes
    adj_codes = claim_data.get("claimAdjudicationCodes", [])
    detail = claim_data.get("claimDetailedInformation", {}) or {}
    lines = detail.get("lines", [])
    line_codes = []
    for line in lines:
        for lc in line.get("lineAdjudicationCodes", []):
            line_codes.append(lc)

    all_codes = adj_codes + line_codes
    denial_reasons = [
        f"{c.get('code', '')}: {c.get('description', '')}"
        for c in all_codes
        if c.get("code")
    ]

    # Check if claim is paid
    if status == "Finalized" and paid > 0:
        return AuthLookupResult(
            found=True, entity=entity, auth_number="",
            reason=(
                f"Optum claim {claim_num}: Finalized, paid ${paid:.2f}. "
                f"No auth issue — claim is paid."
            ),
            raw=claim_data,
        )

    # Denied — report the reason
    reason_summary = "; ".join(denial_reasons[:3]) if denial_reasons else status
    return AuthLookupResult(
        found=False, entity=entity, auth_number="",
        reason=f"Optum claim {claim_num}: {status}. {reason_summary}",
        raw=claim_data,
    )


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


# Map service codes to the Availity 278 requestTypeCode.
# AR = Admission Review (inpatient/residential)
# HS = Health Services Review (outpatient, default)
INPATIENT_SERVICE_CODES = {"RCSU"}
INPATIENT_PROC_CODES = {"H0019", "H2015", "H2016"}


def _request_type_for_claim(claim: Claim) -> str:
    """Return 'AR' for inpatient/residential services, 'HS' otherwise."""
    svc = (getattr(claim, "service_code", "") or "").strip().upper()
    proc = (getattr(claim, "proc_code", "") or "").strip().upper()
    if svc in INPATIENT_SERVICE_CODES or proc in INPATIENT_PROC_CODES:
        return "AR"
    return "HS"


class AvailityServiceReviewInquiry:
    """
    Obtain authorization numbers via Availity 278I Service Review Inquiry.

    Uses GET /v2/service-reviews with NPI + TaxID + DOB + Service Dates
    to discover existing authorizations without knowing the auth number.
    Covers Anthem, Aetna, Molina, Humana — all non-UHC MCOs.
    """

    SR_URL = "https://api.availity.com/availity/development-partner/v2/service-reviews"

    def __init__(self):
        self.client_id = os.getenv("AVAILITY_PROD_CLIENT_ID", "")
        self.client_secret = os.getenv("AVAILITY_PROD_CLIENT_SECRET", "")
        self.base_url = os.getenv("AVAILITY_BASE_URL", "https://api.availity.com")

    async def obtain_authorization(
        self,
        claim: Claim,
        entity: BillingEntity,
    ) -> AuthLookupResult:
        """Query Availity 278I to discover auth number for a claim."""
        await ensure_claim_patient_identity(claim)

        payer_id = AVAILITY_PAYER_IDS.get(claim.mco)
        if not payer_id:
            return AuthLookupResult(
                found=False,
                entity=entity,
                reason=f"Availity payer ID not configured for {claim.mco.value}.",
            )
        if not (self.client_id and self.client_secret):
            return AuthLookupResult(
                found=False,
                entity=entity,
                reason="Availity production credentials not configured.",
            )

        first, last = _claim_name_parts(claim)
        dob = _claim_dob(claim)
        if not (first and last and dob and claim.client_id):
            return AuthLookupResult(
                found=False,
                entity=entity,
                reason="278I inquiry needs member ID, name, DOB, and service dates.",
            )

        # The 278I needs the payer-specific member ID (e.g. Anthem member
        # VAQ...), which differs from the Medicaid ID stored in Lauris.
        # Resolve via Claim.MD 270 eligibility if not already on the claim.
        member_id = str(
            getattr(claim, "payer_member_id", "") or ""
        ).strip()
        if not member_id:
            member_id = await _resolve_payer_member_id(
                claim, entity,
            )
        if not member_id:
            return AuthLookupResult(
                found=False,
                entity=entity,
                reason="Could not resolve payer-specific member ID for 278I.",
            )

        request_type = _request_type_for_claim(claim)
        logger.info(
            "278I request type",
            type=request_type,
            proc=getattr(claim, "proc_code", ""),
            service=getattr(claim, "service_code", ""),
        )

        token = await self._token_value()
        params = {
            "payer.id": payer_id,
            "requestTypeCode": request_type,
            "subscriber.memberId": member_id,
            "subscriber.firstName": first,
            "subscriber.lastName": last,
            "subscriber.birthDate": dob,
            "patient.firstName": first,
            "patient.lastName": last,
            "patient.birthDate": dob,
            "patient.relationshipCode": "18",
            "requestingProvider.npi": entity.billing_npi,
            "requestingProvider.lastName": entity.availity_provider_name,
            "requestingProvider.taxId": entity.tax_id,
            "fromDate": _ymd(claim.dos),
            "toDate": _ymd(claim.dos),
        }
        gender_code = getattr(claim, "gender_code", "")
        if gender_code in ("M", "F"):
            params["patient.genderCode"] = gender_code

        # Humana requires contactName + phone on requestingProvider
        # (enforced on both submission and inquiry endpoints)
        if claim.mco == MCO.HUMANA:
            params["requestingProvider.contactName"] = "LCI Billing"
            params["requestingProvider.phone"] = "7572134272"

        raw = await self._submit_and_poll(token, params)
        return _auth_result_from_service_review(raw, entity, claim)

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
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                data=body,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                data = await resp.json(content_type=None)
                if resp.status >= 400:
                    raise RuntimeError(
                        f"Availity token failed: HTTP {resp.status} {data}"
                    )
                return data["access_token"]

    async def _submit_and_poll(
        self,
        token: str,
        params: dict[str, str],
        max_wait: int = 60,
    ) -> dict[str, Any]:
        headers = {
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
        }
        async with aiohttp.ClientSession() as session:
            async with session.get(
                self.SR_URL,
                params=params,
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=60),
            ) as resp:
                if resp.status == 200:
                    return await resp.json(content_type=None)
                if resp.status != 202:
                    body = await resp.json(content_type=None)
                    return {"_http": resp.status, "_error": body}
                poll_url = resp.headers.get("Location", "")

            if not poll_url:
                return {"_http": 202, "_error": "No Location header in 202 response."}
            # Ensure we use the development-partner path
            if "/availity/development-partner/" not in poll_url:
                poll_url = poll_url.replace(
                    "/v2/service-reviews",
                    "/availity/development-partner/v2/service-reviews",
                )
            if not poll_url.startswith("http"):
                poll_url = f"{self.base_url}{poll_url}"

            deadline = time.time() + max_wait
            while time.time() < deadline:
                await _sleep(2)
                async with session.get(
                    poll_url,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=60),
                ) as poll_resp:
                    if poll_resp.status == 202:
                        continue
                    body = await poll_resp.json(content_type=None)
                    if poll_resp.status == 200:
                        return body
                    return {"_http": poll_resp.status, "_error": body}

        return {"_http": "timeout", "_error": "278I poll timed out."}


def _auth_result_from_service_review(
    raw: dict[str, Any], entity: BillingEntity, claim: Claim,
) -> AuthLookupResult:
    """Parse a 278I response, matching against the claim's service, entity, and DOS.

    An authorization is only considered a match when:
      1. The rendering provider NPI matches the entity that billed the claim.
      2. At least one procedure code on the auth matches the claim's proc code.
      3. The claim's DOS falls within the auth's date range.
    """
    if not raw or raw.get("_error"):
        error_msg = raw.get("_error", "Unknown error") if raw else "Empty response"
        if isinstance(error_msg, dict):
            errors = error_msg.get("errors", [])
            if errors:
                error_msg = errors[0].get("errorMessage", str(error_msg))
        return AuthLookupResult(
            found=False,
            entity=entity,
            reason=f"278I inquiry error: {str(error_msg)[:200]}",
            raw=raw or {},
        )

    reviews = raw.get("serviceReviews", [])
    if not reviews:
        return AuthLookupResult(
            found=False,
            entity=entity,
            reason="278I returned no service reviews for this member/date range.",
            raw=raw,
        )

    claim_proc = (getattr(claim, "proc_code", "") or "").strip().upper()
    claim_dos = claim.dos

    # Score and filter reviews against the claim
    best_match = None
    mismatch_reasons: list[str] = []

    for sr in reviews:
        cert_number = sr.get("certificationNumber", "")
        status = sr.get("status", "")
        status_code = sr.get("statusCode", "")
        from_date_str = sr.get("fromDate", "")
        to_date_str = sr.get("toDate", "")
        service_type = sr.get("serviceType", "")
        procedures = sr.get("procedures", [])
        auth_procs = {p.get("code", "").strip().upper() for p in procedures}
        rendering = sr.get("renderingProviders", [])
        auth_npis = {
            r.get("npi", "").strip() for r in rendering if r.get("npi")
        }

        reasons: list[str] = []

        # 1. Entity/company check — rendering NPI must match
        if auth_npis and entity.billing_npi not in auth_npis:
            reasons.append(
                f"NPI mismatch: auth has {auth_npis}, claim entity is {entity.billing_npi}"
            )

        # 2. Service/procedure check
        if claim_proc and auth_procs and claim_proc not in auth_procs:
            reasons.append(
                f"proc mismatch: auth has {auth_procs}, claim is {claim_proc}"
            )

        # 3. Date range check — claim DOS must fall within auth dates
        try:
            from_date = date.fromisoformat(from_date_str) if from_date_str else None
            to_date = date.fromisoformat(to_date_str) if to_date_str else None
            if claim_dos and from_date and to_date:
                if not (from_date <= claim_dos <= to_date):
                    reasons.append(
                        f"DOS {claim_dos} outside auth range {from_date_str} to {to_date_str}"
                    )
        except ValueError:
            pass

        if reasons:
            mismatch_reasons.append(
                f"{cert_number or '?'}: {'; '.join(reasons)}"
            )
            continue

        # This review matches the claim
        best_match = sr
        break  # Take first matching review

    if not best_match:
        summary = "; ".join(mismatch_reasons[:3])
        return AuthLookupResult(
            found=False,
            entity=entity,
            reason=(
                f"278I returned {len(reviews)} auth(s) but none match "
                f"claim service/entity/dates: {summary}"
            ),
            raw=raw,
        )

    cert_number = best_match.get("certificationNumber", "")
    status = best_match.get("status", "")
    status_code = best_match.get("statusCode", "")
    from_date_str = best_match.get("fromDate", "")
    to_date_str = best_match.get("toDate", "")
    service_type = best_match.get("serviceType", "")
    procedures = best_match.get("procedures", [])
    proc_code = procedures[0].get("code", "") if procedures else ""

    logger.info(
        "278I auth matched",
        cert=cert_number,
        status=status,
        dates=f"{from_date_str} - {to_date_str}",
        service=service_type,
        proc=proc_code,
        entity=entity.key,
        reviews_total=len(reviews),
    )

    return AuthLookupResult(
        found=True,
        entity=entity,
        auth_number=cert_number,
        reason=(
            f"278I: {status} ({status_code}), {from_date_str} to {to_date_str}, "
            f"{service_type}, {proc_code}"
        ),
        raw=raw,
    )


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


def _optum_group_number(claim: Claim) -> str:
    """Return the Optum group number for a UHC claim.

    Virginia CCC+ (Medicaid managed care) uses group 'VACCCP'.
    """
    # All LCI UHC members are Virginia CCC+
    return "VACCCP"


async def _resolve_payer_member_id(
    claim: Claim,
    entity: BillingEntity,
) -> str:
    """Resolve the payer-specific member ID via Claim.MD 270 eligibility.

    For Anthem, the Medicaid ID (from Lauris) differs from the Anthem member
    number (VAQ...).  This function runs a real-time eligibility check and
    returns the ``ins_number`` from the response, which is the payer's own
    member ID.
    """
    elig_payer = ELIG_PAYER_IDS.get(claim.mco)
    if not elig_payer:
        return claim.client_id  # Fallback: use Medicaid ID as-is

    first, last = _claim_name_parts(claim)
    dob = _claim_dob(claim)
    if not (first and last and dob):
        return ""

    try:
        from sources.claimmd_api import ClaimMDAPI

        api = ClaimMDAPI()
        result = await api.check_eligibility(
            member_last=last,
            member_first=first,
            payer_id=elig_payer,
            service_date=_ymd(claim.dos).replace("-", ""),
            provider_npi=entity.billing_npi,
            provider_taxid=entity.tax_id,
            member_id=claim.client_id,
            member_dob=dob.replace("-", ""),
        )
        payer_member = (result.get("elig", {}).get("ins_number", "") or "").strip()
        if payer_member:
            claim.payer_member_id = payer_member
            logger.info(
                "Resolved payer member ID",
                mco=claim.mco.value,
                medicaid_id=claim.client_id,
                payer_member_id=payer_member,
            )
            return payer_member

        logger.warning(
            "Eligibility check returned no payer member ID",
            mco=claim.mco.value,
            result_keys=list(result.keys()),
        )
    except Exception as exc:
        logger.warning(
            "Payer member ID resolution failed",
            mco=claim.mco.value,
            error=str(exc)[:200],
        )
    return ""


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)
