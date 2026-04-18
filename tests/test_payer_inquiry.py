import asyncio
from datetime import date

from config.models import Claim, ClaimStatus, MCO, Program
from sources import payer_inquiry
from sources.payer_inquiry import (
    PayerInquiryResult,
    check_payer_claim_status,
    ensure_claim_patient_identity,
    is_billed_rejected_or_denied,
)


def _claim(status=ClaimStatus.DENIED, mco=MCO.SENTARA, billed=100.0):
    return Claim(
        claim_id="C123",
        client_name="Jane Doe",
        client_id="M123",
        dos=date(2026, 1, 15),
        mco=mco,
        program=Program.NHCS,
        billed_amount=billed,
        status=status,
    )


def test_scope_only_billed_rejected_or_denied_claims_pass():
    assert is_billed_rejected_or_denied(_claim(status=ClaimStatus.DENIED))
    assert is_billed_rejected_or_denied(_claim(status=ClaimStatus.REJECTED))

    assert not is_billed_rejected_or_denied(_claim(status=ClaimStatus.PAID))
    assert not is_billed_rejected_or_denied(_claim(status=ClaimStatus.PENDING))
    assert not is_billed_rejected_or_denied(_claim(status=ClaimStatus.DENIED, billed=0.0))


def test_united_claims_use_optum(monkeypatch):
    calls = []

    class FakeOptum:
        async def check_claim(self, claim):
            calls.append(("optum", claim.claim_id))
            return PayerInquiryResult(
                gateway="optum",
                bucket="real_denial",
                ok=True,
                should_process=True,
                reason="Optum says work it.",
            )

    class FakeAvaility:
        async def check_claim(self, claim):
            calls.append(("availity", claim.claim_id))
            raise AssertionError("United claims should not go to Availity")

    monkeypatch.setattr(payer_inquiry, "OptumClaimInquiryClient", FakeOptum)
    monkeypatch.setattr(payer_inquiry, "AvailityClaimStatusClient", FakeAvaility)

    result = asyncio.run(check_payer_claim_status(_claim(mco=MCO.UNITED)))

    assert result.gateway == "optum"
    assert calls == [("optum", "C123")]


def test_non_united_claims_use_availity(monkeypatch):
    calls = []

    class FakeOptum:
        async def check_claim(self, claim):
            calls.append(("optum", claim.claim_id))
            raise AssertionError("Non-United claims should not go to Optum")

    class FakeAvaility:
        async def check_claim(self, claim):
            calls.append(("availity", claim.claim_id))
            return PayerInquiryResult(
                gateway="availity",
                bucket="real_denial",
                ok=True,
                should_process=True,
                reason="Availity says work it.",
            )

    monkeypatch.setattr(payer_inquiry, "OptumClaimInquiryClient", FakeOptum)
    monkeypatch.setattr(payer_inquiry, "AvailityClaimStatusClient", FakeAvaility)

    result = asyncio.run(check_payer_claim_status(_claim(mco=MCO.AETNA)))

    assert result.gateway == "availity"
    assert calls == [("availity", "C123")]


def test_eligibility_fallback_sets_claim_dob(monkeypatch):
    claim = _claim(mco=MCO.SENTARA)
    claim.patient_first_name = "JOSHUA"
    claim.patient_last_name = "SHAW"
    claim.npi = "1306491592"

    class FakeClaimMDAPI:
        key = "configured"

        async def check_eligibility(self, **kwargs):
            return {"ins_dob": "1980-04-01"}

    monkeypatch.setattr("sources.claimmd_api.ClaimMDAPI", FakeClaimMDAPI)

    asyncio.run(ensure_claim_patient_identity(claim))

    assert claim.client_dob == "1980-04-01"
