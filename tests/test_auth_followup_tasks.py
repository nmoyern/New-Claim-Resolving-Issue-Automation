import asyncio
from datetime import date

import actions.clickup_tasks
from actions.auth_followup_tasks import (
    create_missing_auth_clickup_tasks,
    group_claims_by_unique_id,
    needs_authorization_before_resubmission,
)
from config.models import Claim, ClaimStatus, DenialCode, MCO, Program


def _claim(claim_id, unique_id="ID004665", proc_code="H2015", program=Program.NHCS):
    return Claim(
        claim_id=claim_id,
        client_name="Jane Doe",
        client_id="M123",
        lauris_id=unique_id,
        dos=date(2026, 1, 15),
        mco=MCO.SENTARA,
        program=program,
        billed_amount=100.0,
        status=ClaimStatus.DENIED,
        denial_codes=[DenialCode.NO_AUTH],
        denial_reason_raw="No authorization on file",
        proc_code=proc_code,
        service_code="RCSU",
    )


def test_missing_auth_gate_requires_no_auth_denial_and_blank_auth():
    claim = _claim("C1")
    assert needs_authorization_before_resubmission(claim)

    claim.auth_number = "AUTH123"
    assert not needs_authorization_before_resubmission(claim)

    claim.auth_number = ""
    claim.denial_codes = [DenialCode.INVALID_ID]
    assert not needs_authorization_before_resubmission(claim)


def test_group_claims_by_unique_id_combines_multiple_claim_lines():
    groups = group_claims_by_unique_id([
        _claim("C1", unique_id="ID004665", proc_code="H2015"),
        _claim("C2", unique_id="ID004665", proc_code="H0031"),
        _claim("C3", unique_id="ID009999", proc_code="H2019"),
    ])

    assert len(groups) == 2
    first = next(group for group in groups if group.unique_id == "ID004665")
    assert [claim.claim_id for claim in first.claims] == ["C1", "C2"]


def test_create_missing_auth_clickup_tasks_lists_cpt_and_program(monkeypatch):
    calls = []

    class FakeCreator:
        async def create_or_update_patient_task(self, **kwargs):
            calls.append(kwargs)
            return f"task-{len(calls)}"

    monkeypatch.setattr(actions.clickup_tasks, "ClickUpTaskCreator", FakeCreator)

    task_ids = asyncio.run(create_missing_auth_clickup_tasks([
        _claim("C1", unique_id="ID004665", proc_code="H2015", program=Program.NHCS),
        _claim("C2", unique_id="ID004665", proc_code="H0031", program=Program.NHCS),
    ]))

    assert task_ids == {"C1": "task-1", "C2": "task-1"}
    assert calls[0]["patient_key"] == "ID004665"
    assert calls[0]["client_id"] == "ID004665"
    assert "DOS 2026-01-15" in calls[0]["task_name"]
    assert "Date(s) of service in this request: 2026-01-15" in calls[0]["issue"]
    assert "CPT H2015" in calls[0]["issue"]
    assert "CPT H0031" in calls[0]["issue"]
    assert "Program NHCS" in calls[0]["issue"]
    assert "Claim C1 | DOS 2026-01-15" in calls[0]["history"]
    assert "before this claim is resubmitted" in calls[0]["needed"]
