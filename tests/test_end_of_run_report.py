from datetime import date

from config.models import Claim, ClaimStatus, DailyRunSummary, DenialCode, MCO, Program, ResolutionAction, ResolutionResult
from reporting.end_of_run_report import build_ar_lookup, build_claim_run_row, generate_end_of_run_report


def _claim():
    return Claim(
        claim_id="C123",
        client_name="Jane Doe",
        client_id="M123",
        lauris_id="ID004665",
        dos=date(2026, 1, 15),
        mco=MCO.SENTARA,
        program=Program.NHCS,
        billed_amount=125.0,
        paid_amount=0.0,
        status=ClaimStatus.DENIED,
        denial_codes=[DenialCode.NO_AUTH, DenialCode.INVALID_ID],
        proc_code="H2015",
        service_code="RCSU",
        denial_reason_raw="No authorization on file",
    )


def test_build_claim_run_row_includes_outstanding_and_clickup():
    result = ResolutionResult(
        claim=_claim(),
        action_taken=ResolutionAction.HUMAN_REVIEW,
        success=False,
        needs_human=True,
        human_reason="Authorization missing before resubmission.",
    )
    ar_lookup = build_ar_lookup([
        {
            "member_id": "M123",
            "doc_date": "2026-01-15",
            "total_received": 25.0,
            "outstanding": 100.0,
            "ar_status": "Under Payment",
        }
    ])

    row = build_claim_run_row(result, ar_lookup=ar_lookup, clickup_task_id="CU-123")

    assert row["paid_amount"] == 25.0
    assert row["outstanding_balance"] == 100.0
    assert row["clickup_task_id"] == "CU-123"
    assert row["cpt_code"] == "H2015"
    assert row["denial_codes"] == ["no_auth_on_file", "invalid_id"]
    assert row["payer_api_detail_summary"] == ""
    assert row["human_needed"] is True


def test_generate_end_of_run_report_writes_summary(tmp_path, monkeypatch):
    import reporting.end_of_run_report as end_of_run_report

    writes = []

    class FakeWrite:
        def __init__(self, name):
            self.local_path = tmp_path / name
            self.dropbox_path = f"/Chesapeake LCI/AR Reports/Claim Resolution/Daily_Run_Reports/{name}"
            self.uploaded_to_dropbox = False
            self.upload_error = ""

    def fake_write_text_report(report_type, prefix, suffix, content, **kwargs):
        name = f"{prefix}{suffix}"
        path = tmp_path / name
        path.write_text(content)
        writes.append((report_type, prefix, suffix, content))
        return FakeWrite(name)

    monkeypatch.setattr(end_of_run_report, "write_text_report", fake_write_text_report)

    summary = DailyRunSummary(results=[
        ResolutionResult(
            claim=_claim(),
            action_taken=ResolutionAction.CORRECT_AND_RESUBMIT,
            success=True,
            note_written="Corrected and resubmitted automatically.",
        )
    ])
    summary.results[0].claim.payer_api_detail_summary = "F2 - Denied - Authorization missing"
    summary.results[0].claim.payer_api_reason = "Availity confirms denial."

    report = generate_end_of_run_report(
        summary,
        ar_lookup=build_ar_lookup([
            {
                "member_id": "M123",
                "doc_date": "2026-01-15",
                "total_received": 25.0,
                "outstanding": 100.0,
                "ar_status": "Under Payment",
            }
        ]),
        clickup_task_map={"C123": "CU-123"},
    )

    assert report["totals"]["outstanding_total"] == 100.0
    assert report["claims"][0]["clickup_task_id"] == "CU-123"
    assert report["claims"][0]["auto_fixed"] is True
    assert report["claims"][0]["payer_api_detail_summary"] == "F2 - Denied - Authorization missing"
    assert report["output"]["markdown_path"].endswith("daily_run_report.md")
    assert any("Total outstanding / balance due: $100.00" in item[3] for item in writes if item[2] == ".md")
    assert any("Denial Codes" in item[3] for item in writes if item[2] == ".md")
    assert any("Payer API Findings" in item[3] for item in writes if item[2] == ".md")
