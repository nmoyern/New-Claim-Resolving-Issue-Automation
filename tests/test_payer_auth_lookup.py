import asyncio
from datetime import date

from actions.company_auth_match import AuthLookupResult
from config.entities import get_entity_by_program
from config.models import Claim, ClaimStatus, MCO, Program
from sources.payer_auth_lookup import (
    PayerAuthorizationLookup,
    _auth_result_from_availity,
    _auth_result_from_optum,
    _claim_dob,
    _claim_name_parts,
)


class FakeLookup:
    def __init__(self, label):
        self.label = label
        self.calls = []

    async def check_authorization(self, claim, entity):
        self.calls.append((claim.mco, entity.key))
        return AuthLookupResult(
            found=True,
            entity=entity,
            auth_number=f"{self.label}-AUTH",
            reason=f"{self.label} called",
        )


def _claim(mco=MCO.AETNA):
    return Claim(
        claim_id="C123",
        client_name="Jane Doe",
        client_id="M123",
        dos=date(2026, 1, 15),
        mco=mco,
        program=Program.NHCS,
        billed_amount=100.0,
        status=ClaimStatus.DENIED,
        npi="1700297447",
    )


def test_routes_united_to_optum_lookup():
    lookup = PayerAuthorizationLookup()
    lookup.optum = FakeLookup("optum")
    lookup.availity = FakeLookup("availity")
    entity = get_entity_by_program(Program.NHCS)

    result = asyncio.run(lookup.check_authorization(_claim(MCO.UNITED), entity))

    assert result.auth_number == "optum-AUTH"
    assert lookup.optum.calls == [(MCO.UNITED, "NHCS")]
    assert lookup.availity.calls == []


def test_routes_non_united_to_availity_lookup():
    lookup = PayerAuthorizationLookup()
    lookup.optum = FakeLookup("optum")
    lookup.availity = FakeLookup("availity")
    entity = get_entity_by_program(Program.NHCS)

    result = asyncio.run(lookup.check_authorization(_claim(MCO.AETNA), entity))

    assert result.auth_number == "availity-AUTH"
    assert lookup.optum.calls == []
    assert lookup.availity.calls == [(MCO.AETNA, "NHCS")]


def test_optum_auth_response_with_case_summary_is_found():
    entity = get_entity_by_program(Program.NHCS)
    raw = {
        "status_code": 200,
        "body": {
            "data": {
                "searchPriorAuths": {
                    "caseSummary": [
                        {"serviceReferenceNumber": "AUTH123"}
                    ],
                    "caseDescription": [],
                }
            }
        },
    }

    result = _auth_result_from_optum(raw, entity)

    assert result.found is True
    assert result.auth_number == "AUTH123"
    assert result.entity.key == "NHCS"


def test_optum_empty_response_is_not_found():
    entity = get_entity_by_program(Program.NHCS)
    raw = {
        "status_code": 200,
        "body": {
            "data": {
                "searchPriorAuths": {
                    "caseSummary": [],
                    "caseDescription": [],
                }
            }
        },
    }

    result = _auth_result_from_optum(raw, entity)

    assert result.found is False
    assert result.auth_number == ""


def test_availity_d0_only_is_not_a_match():
    entity = get_entity_by_program(Program.KJLN)
    raw = {
        "claimStatuses": [
            {"statusDetails": [{"categoryCode": "D0"}]}
        ]
    }

    result = _auth_result_from_availity(raw, entity)

    assert result.found is False


def test_availity_non_d0_record_is_a_match():
    entity = get_entity_by_program(Program.KJLN)
    raw = {
        "claimStatuses": [
            {"statusDetails": [{"categoryCode": "F2"}]}
        ]
    }

    result = _auth_result_from_availity(raw, entity)

    assert result.found is True
    assert result.entity.key == "KJLN"


def test_claim_dob_reads_dynamic_enrichment_field():
    claim = _claim()
    claim.client_dob = "2000-01-02"

    assert _claim_dob(claim) == "2000-01-02"


def test_claim_name_parts_prefers_lauris_enrichment():
    claim = _claim()
    claim.client_name = "CW4178-1176811"
    claim.patient_first_name = "JANE"
    claim.patient_last_name = "DOE"

    assert _claim_name_parts(claim) == ("JANE", "DOE")
