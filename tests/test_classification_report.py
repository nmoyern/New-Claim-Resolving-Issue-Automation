import asyncio
from datetime import date
from types import SimpleNamespace

from config.models import Claim, ClaimStatus, DenialCode, MCO, Program
from reporting import classification_report
from reporting.classification_report import (
    build_claim_classification,
    claim_previous_context,
    render_markdown,
    write_report_files,
)
from sources.payer_inquiry import PayerInquiryResult


def _claim():
    claim = Claim(
        claim_id="C123",
        client_name="Jane Doe",
        client_id="M123",
        dos=date(2026, 1, 15),
        mco=MCO.SENTARA,
        program=Program.NHCS,
        billed_amount=125.0,
        status=ClaimStatus.DENIED,
        denial_codes=[DenialCode.NO_AUTH],
        denial_reason_raw="No authorization on file",
        auth_number="AUTH1",
        npi="1700297447",
        billing_region="NHCS",
        lauris_id="ID004665",
        proc_code="H2015",
        date_billed=date(2026, 1, 16),
        date_denied=date(2026, 1, 20),
        last_note="Prior follow-up note",
    )
    claim.client_dob = "2010-01-01"
    claim.gender_code = "F"
    return claim


def test_previous_context_keeps_old_claim_details():
    context = claim_previous_context(_claim())

    assert context["status"] == "denied"
    assert context["denial_codes"] == ["no_auth_on_file"]
    assert context["denial_reason_raw"] == "No authorization on file"
    assert context["auth_number"] == "AUTH1"
    assert context["npi"] == "1700297447"
    assert context["lauris_id"] == "ID004665"
    assert context["client_dob"] == "2010-01-01"


def test_build_claim_classification_records_each_step(monkeypatch):
    async def fake_payer_status(claim):
        return PayerInquiryResult(
            gateway="availity",
            bucket="real_denial",
            ok=True,
            should_process=True,
            reason="Availity confirms denial.",
        )

    async def fake_company_auth(claim):
        return SimpleNamespace(
            claim_id=claim.claim_id,
            status="mismatch_single_match",
            current_entity=SimpleNamespace(key="nhcs"),
            matched_entities=[],
            recommended_action="update_to_kjln_and_resubmit",
            reason="Authorization matches KJLN, not NHCS.",
            fields_to_change={
                "billing_region": "KJLN",
                "npi": "1306491592",
                "tax_id": "821966562",
                "auth_number": "AUTH2",
            },
            should_update_claim=True,
            needs_human=False,
        )

    monkeypatch.setattr(classification_report, "check_payer_claim_status", fake_payer_status)
    monkeypatch.setattr(classification_report, "classify_with_payer_lookup", fake_company_auth)

    result = asyncio.run(build_claim_classification(_claim()))

    assert result["payer_status"]["bucket"] == "real_denial"
    assert result["company_auth_match"]["status"] == "mismatch_single_match"
    assert result["company_auth_match"]["fields_to_change"]["tax_id"] == "821966562"
    assert result["recommended_action"] == "Update the billing company/EIN/NPI/auth fields, then resubmit."
    assert [step["step"] for step in result["steps"]] == [
        "scope_filter",
        "lauris_demographics",
        "payer_status",
        "company_auth_match",
        "decision_tree_route",
    ]


def test_report_files_include_readable_summary(tmp_path):
    report = {
        "metadata": {
            "started_at": "2026-04-17T09:00:00",
            "full_pull": False,
            "max_claims": 1,
            "include_payer_api": False,
            "mutates_claimmd": False,
            "posts_eras": False,
            "advances_claimmd_response_cursor": False,
        },
        "era_posture": {
            "normal_live_position": "ERA runs first.",
            "dry_run_action": "Not executed.",
            "why": "Read-only proof report.",
        },
        "counts": {
            "claimmd_rejected_denied_seen": 1,
            "billed_rejected_denied_in_scope": 1,
            "included_in_report": 1,
            "skipped_by_scope": 0,
            "skipped_by_limit": 0,
            "demographics_enriched": 1,
            "demographics_missing": 0,
        },
        "claims": [
            {
                "claim": {
                    "claim_id": "C123",
                    "client_name": "Jane Doe",
                    "unique_id": "ID004665",
                    "member_id": "M123",
                    "dos": "2026-01-15",
                    "mco": "sentara",
                    "program": "NHCS",
                    "billed_amount": 125.0,
                    "paid_amount": 0.0,
                    "claimmd_url": "",
                    "cpt_code": "H2015",
                    "service_code": "RCSU",
                },
                "previous_context": claim_previous_context(_claim()),
                "steps": [
                    {
                        "step": "scope_filter",
                        "result": "included",
                        "plain_english": "Claim was billed.",
                    }
                ],
                "payer_status": None,
                "company_auth_match": None,
                "router": {"action": "mco_portal_auth_check", "reason": "no_auth_on_file"},
                "recommended_action": "Proceed with mco_portal_auth_check.",
                "human_needed": False,
            }
        ],
    }

    json_path, md_path = write_report_files(report, output_dir=tmp_path)
    markdown = render_markdown(report)

    assert json_path.exists()
    assert md_path.exists()
    assert "Claim Classification Dry Run" in markdown
    assert "Unique ID: ID004665" in markdown
    assert "CPT code/program: H2015 / NHCS" in markdown
    assert "Did not post ERAs" in markdown
