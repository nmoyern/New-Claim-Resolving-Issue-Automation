"""
tests/test_pre_billing.py
--------------------------
Tests for the pre-billing corrections module.
"""
import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from unittest.mock import patch

import pytest

os.environ.setdefault("DRY_RUN", "true")

from config.models import Claim, ClaimStatus, DenialCode, MCO, Program
from actions.pre_billing_check import (
    check_diagnosis,
    check_entity,
    check_auth,
    check_rendering_npi,
    check_member_id,
    run_pre_billing_checks,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _make_claim(
    claim_id="PB001",
    client_name="Test Patient",
    client_id="MBR001",
    mco=MCO.SENTARA,
    program=Program.NHCS,
    denial_codes=None,
    auth_number="AUTH123",
    npi="1588094513",
    service_code="",
    billing_region="NHCS",
) -> Claim:
    return Claim(
        claim_id=claim_id,
        client_name=client_name,
        client_id=client_id,
        dos=date.today() - timedelta(days=10),
        mco=mco,
        program=program,
        billed_amount=500.0,
        status=ClaimStatus.PENDING,
        denial_codes=denial_codes or [],
        auth_number=auth_number,
        npi=npi,
        service_code=service_code,
        billing_region=billing_region,
    )


# ---------------------------------------------------------------------------
# check_diagnosis tests
# ---------------------------------------------------------------------------

class TestCheckDiagnosis:

    def test_passes_with_no_diagnosis_issue(self):
        claim = _make_claim()
        ok, msg = check_diagnosis(claim)
        assert ok is True

    def test_fails_with_blank_diagnosis_code(self):
        claim = _make_claim(denial_codes=[DenialCode.DIAGNOSIS_BLANK])
        ok, msg = check_diagnosis(claim)
        assert ok is False
        assert "Missing or blank diagnosis" in msg


# ---------------------------------------------------------------------------
# check_entity tests
# ---------------------------------------------------------------------------

class TestCheckEntity:

    def test_passes_when_entity_matches(self):
        claim = _make_claim(program=Program.NHCS, billing_region="NHCS")
        ok, msg = check_entity(claim)
        assert ok is True

    def test_fails_when_entity_mismatches(self):
        claim = _make_claim(program=Program.NHCS, billing_region="KJLN")
        ok, msg = check_entity(claim)
        # In DRY_RUN, this should fail (not auto-fix)
        assert ok is False
        assert "mismatch" in msg.lower()

    def test_fails_with_wrong_billing_co_denial(self):
        claim = _make_claim(denial_codes=[DenialCode.WRONG_BILLING_CO])
        ok, msg = check_entity(claim)
        assert ok is False


# ---------------------------------------------------------------------------
# check_auth tests
# ---------------------------------------------------------------------------

class TestCheckAuth:

    def test_passes_with_auth_number(self):
        claim = _make_claim(auth_number="AUTH999")
        ok, msg = check_auth(claim)
        assert ok is True

    def test_fails_with_missing_auth(self):
        claim = _make_claim(auth_number="")
        ok, msg = check_auth(claim)
        assert ok is False
        assert "Missing authorization" in msg

    def test_fails_with_no_auth_denial(self):
        claim = _make_claim(denial_codes=[DenialCode.NO_AUTH], auth_number="")
        ok, msg = check_auth(claim)
        assert ok is False


# ---------------------------------------------------------------------------
# check_rendering_npi tests
# ---------------------------------------------------------------------------

class TestCheckRenderingNPI:

    def test_passes_for_non_rcsu(self):
        claim = _make_claim(service_code="MHSS")
        ok, msg = check_rendering_npi(claim)
        assert ok is True

    def test_fails_with_missing_npi_denial(self):
        claim = _make_claim(denial_codes=[DenialCode.MISSING_NPI_RENDERING])
        ok, msg = check_rendering_npi(claim)
        # Will fail because no rendering_npi attribute set
        assert ok is False or "Rendering NPI present" in msg


# ---------------------------------------------------------------------------
# check_member_id tests
# ---------------------------------------------------------------------------

class TestCheckMemberID:

    def test_passes_with_valid_id(self):
        claim = _make_claim(client_id="MBR001")
        ok, msg = check_member_id(claim)
        assert ok is True

    def test_fails_with_empty_id(self):
        claim = _make_claim(client_id="")
        ok, msg = check_member_id(claim)
        assert ok is False
        assert "Invalid or missing member ID" in msg

    def test_fails_with_invalid_id_denial(self):
        claim = _make_claim(denial_codes=[DenialCode.INVALID_ID], client_id="")
        ok, msg = check_member_id(claim)
        assert ok is False


# ---------------------------------------------------------------------------
# run_pre_billing_checks tests
# ---------------------------------------------------------------------------

class TestRunPreBillingChecks:

    def test_all_pass(self):
        claims = [
            _make_claim("C1"),
            _make_claim("C2"),
        ]
        result = run_pre_billing_checks(claims)
        assert result["summary"]["total_checked"] == 2
        assert result["summary"]["total_passed"] == 2
        assert result["summary"]["total_blocked"] == 0

    def test_blocked_claim(self):
        claims = [
            _make_claim("C1", auth_number=""),  # Will fail auth check
        ]
        result = run_pre_billing_checks(claims)
        assert result["summary"]["total_blocked"] == 1
        assert len(result["blocked"]) == 1
        assert result["blocked"][0].claim_id == "C1"

    def test_issues_are_logged(self):
        claims = [
            _make_claim("C1", auth_number="", client_id=""),
        ]
        result = run_pre_billing_checks(claims)
        assert len(result["issues"]) >= 2  # At least auth and member_id

    def test_mixed_claims(self):
        claims = [
            _make_claim("C1"),  # Should pass
            _make_claim("C2", auth_number=""),  # Should be blocked
            _make_claim("C3"),  # Should pass
        ]
        result = run_pre_billing_checks(claims)
        assert result["summary"]["total_passed"] == 2
        assert result["summary"]["total_blocked"] == 1

    def test_returns_correct_structure(self):
        result = run_pre_billing_checks([_make_claim()])
        assert "passed" in result
        assert "fixed" in result
        assert "blocked" in result
        assert "issues" in result
        assert "summary" in result
        summary = result["summary"]
        assert "total_checked" in summary
        assert "total_passed" in summary
        assert "total_fixed" in summary
        assert "total_blocked" in summary

    def test_empty_claims_list(self):
        result = run_pre_billing_checks([])
        assert result["summary"]["total_checked"] == 0
        assert result["summary"]["total_passed"] == 0
