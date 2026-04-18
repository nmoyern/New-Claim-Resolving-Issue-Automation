from datetime import date

from config.models import Claim, ClaimStatus, MCO, Program
from sources.payer_inquiry import (
    AvailityClaimStatusClient,
    OptumClaimInquiryClient,
    attach_payer_api_details_to_claim,
)


def _claim(mco=MCO.UNITED):
    return Claim(
        claim_id="C123",
        client_name="Jane Doe",
        client_id="M123",
        dos=date(2026, 1, 15),
        mco=mco,
        program=Program.NHCS,
        billed_amount=100.0,
        status=ClaimStatus.DENIED,
    )


def test_optum_classify_includes_status_and_adjudication_details():
    raw = {
        "status_code": 200,
        "body": {
            "data": {
                "searchClaim": {
                    "claims": [
                        {
                            "claimStatus": "Denied",
                            "claimLevelTotalAmount": {"totalPaidAmount": "0"},
                            "claimStatusCrosswalkData": {
                                "claim507CodeDesc": "Denied by payer",
                                "claim508CodeDesc": "Authorization missing",
                            },
                            "claimAdjudicationCodes": [
                                {
                                    "claimCodeType": "CARC",
                                    "code": "197",
                                    "description": "Precertification/authorization/notification absent",
                                }
                            ],
                        }
                    ]
                }
            }
        },
    }

    result = OptumClaimInquiryClient()._classify(_claim(), raw)

    assert result.bucket == "real_denial"
    assert "Claim status: Denied" in result.detail_summary
    assert "Authorization missing" in result.detail_summary
    assert any("197" in item for item in result.detail_items)


def test_availity_classify_includes_detail_descriptions():
    raw = {
        "claimStatuses": [
            {
                "statusDetails": [
                    {
                        "categoryCode": "F2",
                        "statusCode": "A1",
                        "statusCodeDescription": "Finalized/Denied",
                        "paymentAmount": "0",
                    }
                ]
            }
        ]
    }

    result = AvailityClaimStatusClient()._classify(raw)

    assert result.bucket == "real_denial"
    assert "F2 - A1 - Finalized/Denied" in result.detail_summary


def test_attach_payer_api_details_to_claim_sets_report_fields():
    raw = {
        "claimStatuses": [
            {
                "statusDetails": [
                    {
                        "categoryCode": "F2",
                        "statusCode": "A1",
                        "statusCodeDescription": "Finalized/Denied",
                        "paymentAmount": "0",
                    }
                ]
            }
        ]
    }
    claim = _claim(mco=MCO.SENTARA)
    result = AvailityClaimStatusClient()._classify(raw)

    attach_payer_api_details_to_claim(claim, result)

    assert claim.payer_api_bucket == "real_denial"
    assert "Finalized/Denied" in claim.payer_api_detail_summary
