import asyncio
from datetime import date

from actions.company_auth_match import (
    AuthLookupResult,
    classify_company_auth_match,
    infer_claim_entity,
)
from config.models import Claim, ClaimStatus, MCO, Program


class FakeAuthLookup:
    def __init__(self, matches):
        self.matches = matches
        self.checked = []

    async def check_authorization(self, claim, entity):
        self.checked.append(entity.key)
        match = self.matches.get(entity.key)
        return AuthLookupResult(
            found=bool(match),
            entity=entity,
            auth_number=match or "",
            reason="fake test lookup",
        )


def _claim(program=Program.MARYS_HOME, npi="1437871753", billing_region="Mary's Home Inc"):
    return Claim(
        claim_id="C123",
        client_name="Jane Doe",
        client_id="M123",
        dos=date(2026, 1, 15),
        mco=MCO.AETNA,
        program=program,
        billed_amount=100.0,
        status=ClaimStatus.DENIED,
        npi=npi,
        billing_region=billing_region,
    )


def test_infers_current_entity_from_npi_first():
    claim = _claim(
        program=Program.MARYS_HOME,
        npi="1700297447",
        billing_region="Mary's Home Inc",
    )

    assert infer_claim_entity(claim).key == "NHCS"


def test_current_entity_match_continues_normal_workflow():
    lookup = FakeAuthLookup({"MARYS_HOME": "AUTH1"})

    result = asyncio.run(classify_company_auth_match(_claim(), lookup))

    assert result.status == "current_entity_match"
    assert result.recommended_action == "continue_normal_denial_workflow"
    assert result.should_update_claim is False
    assert result.needs_human is False
    assert lookup.checked == ["MARYS_HOME"]


def test_single_different_entity_match_recommends_update_fields():
    lookup = FakeAuthLookup({"NHCS": "AUTH2"})

    result = asyncio.run(classify_company_auth_match(_claim(), lookup))

    assert result.status == "mismatch_single_match"
    assert result.should_update_claim is True
    assert result.recommended_action == "update_to_NHCS_and_resubmit"
    assert result.fields_to_change == {
        "billing_region": "NHCS",
        "npi": "1700297447",
        "tax_id": "465232420",
        "auth_number": "AUTH2",
    }
    assert lookup.checked == ["MARYS_HOME", "NHCS", "KJLN"]


def test_no_entity_match_goes_to_human_review():
    lookup = FakeAuthLookup({})

    result = asyncio.run(classify_company_auth_match(_claim(), lookup))

    assert result.status == "no_auth_match"
    assert result.needs_human is True
    assert result.recommended_action == "human_review"
    assert result.fields_to_change == {}


def test_multiple_entity_matches_go_to_human_review():
    lookup = FakeAuthLookup({"NHCS": "AUTH2", "KJLN": "AUTH3"})

    result = asyncio.run(classify_company_auth_match(_claim(), lookup))

    assert result.status == "multiple_auth_matches"
    assert result.needs_human is True
    assert result.recommended_action == "human_review"
    assert [m.entity.key for m in result.matched_entities] == ["NHCS", "KJLN"]


def test_unknown_current_entity_can_still_find_single_match():
    lookup = FakeAuthLookup({"KJLN": "AUTH3"})
    claim = _claim(program=Program.UNKNOWN, npi="", billing_region="")

    result = asyncio.run(classify_company_auth_match(claim, lookup))

    assert result.current_entity is None
    assert result.status == "mismatch_single_match"
    assert result.fields_to_change["billing_region"] == "KJLN"
    assert result.fields_to_change["npi"] == "1306491592"
    assert result.fields_to_change["tax_id"] == "821966562"
