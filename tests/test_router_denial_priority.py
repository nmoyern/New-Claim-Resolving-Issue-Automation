from datetime import date

from config.models import Claim, ClaimStatus, DenialCode, MCO, Program, ResolutionAction
from decision_tree.router import ClaimRouter


def _claim(denial_codes):
    return Claim(
        claim_id="C123",
        client_name="Jane Doe",
        client_id="M123",
        dos=date(2026, 1, 15),
        mco=MCO.SENTARA,
        program=Program.NHCS,
        billed_amount=100.0,
        status=ClaimStatus.DENIED,
        denial_codes=denial_codes,
    )


def test_route_considers_all_denial_codes_not_just_first():
    claim = _claim([DenialCode.NO_AUTH, DenialCode.INVALID_ID])
    action, reason = ClaimRouter().route(claim)

    assert action == ResolutionAction.CORRECT_AND_RESUBMIT
    assert reason == "incorrect_member_id"


def test_route_prefers_claim_correction_before_auth_check():
    claim = _claim([DenialCode.AUTH_EXPIRED, DenialCode.DIAGNOSIS_BLANK])
    action, reason = ClaimRouter().route(claim)

    assert action == ResolutionAction.CORRECT_AND_RESUBMIT
    assert reason == "diagnosis_blank_clickup_then_fix"
