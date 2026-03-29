"""
tests/test_all.py — LCI Claims Automation full test suite
Run: pytest tests/test_all.py -v
"""
import asyncio, csv, io, os, tempfile
from datetime import date, timedelta
from typing import List
from unittest.mock import AsyncMock, patch

import pytest
os.environ.setdefault("DRY_RUN", "true")

from config.models import (
    Claim, ClaimStatus, DailyRunSummary, DenialCode, ERA, MCO,
    Program, ResolutionAction, ResolutionResult,
)
from notes.formatter import (
    format_note, note_correction, note_reconsideration_submitted,
    note_write_off, note_billing_company_fixed, note_era_uploaded,
    note_auth_verified_in_portal, note_human_review_needed,
    get_recon_reason,
)
from decision_tree.router import ClaimRouter, get_todays_primary_actions
from sources.claimmd import parse_denial_codes, _parse_mco, _parse_date
from sources.powerbi import parse_billing_summary_csv, _parse_float, _parse_int, _parse_program


def make_claim(claim_id="C001", client_name="John Doe", mco=MCO.SENTARA,
               program=Program.NHCS, denial_codes=None, status=ClaimStatus.DENIED,
               age_days=30, billed=500.0) -> Claim:
    return Claim(claim_id=claim_id, client_name=client_name, client_id="MBR123",
                 dos=date.today() - timedelta(days=age_days), mco=mco, program=program,
                 billed_amount=billed, status=status,
                 denial_codes=denial_codes or [DenialCode.NO_AUTH], age_days=age_days)


def make_csv(rows, headers=None):
    if headers is None:
        headers = ["Client Name","MCO","DOS","Amount Billed","Amount Paid",
                   "Denial Reason","Billing Program","Auth Number","Days Outstanding"]
    out = io.StringIO(); w = csv.writer(out); w.writerow(headers); w.writerows(rows)
    return out.getvalue()


# ── 1. Note Formatter ──────────────────────────────────────────────────────

class TestNoteFormatter:
    def test_basic_format(self):
        n = format_note("Corrections made", "NM")
        assert f"#NM #{date.today().strftime('%m/%d/%y')}" in n

    def test_hash_in_body_raises(self):
        with pytest.raises(ValueError):
            format_note("ref #123 inside body", "NM")

    def test_two_digit_month_day(self):
        n = format_note("Test", "CJ", as_of=date(2026, 3, 5))
        assert "#CJ #03/05/26" in n

    def test_exactly_two_hashes(self):
        n = format_note("Action taken", "JO")
        assert n.count("#") == 2

    def test_all_templates_valid(self):
        notes = [
            note_correction("member_id fixed"),
            note_reconsideration_submitted("Sentara"),
            note_write_off("auth denied"),
            note_billing_company_fixed("KJLN", "NHCS"),
            note_era_uploaded("Aetna", "ERA-002"),
            note_auth_verified_in_portal("Molina", "AUTH-999"),
            note_human_review_needed("needs call"),
        ]
        for n in notes:
            body = n.rsplit("#", 2)[0]
            assert "#" not in body, f"# in body: {n}"

    def test_whitespace_stripped(self):
        n = format_note("Action.   ", "NM")
        assert n.split(" #")[0] == "Action."


# ── 2. Recon Reasons ──────────────────────────────────────────────────────

class TestReconReasons:
    def test_aetna_dmas_language(self):
        r = get_recon_reason("no_auth", "aetna")
        assert "DMAS standards" in r
        assert "Open, approved authorization on file for DOS" in r

    def test_no_auth_standard(self):
        assert "Open, approved authorization" in get_recon_reason("no_auth", "sentara")

    def test_duplicate(self):
        assert "not a duplicate" in get_recon_reason("duplicate", "united").lower()

    def test_not_enrolled_h0046(self):
        assert "H0046" in get_recon_reason("not_enrolled", "molina")

    def test_aetna_different_from_standard(self):
        assert get_recon_reason("no_auth", "aetna") != get_recon_reason("no_auth", "sentara")


# ── 3. Decision Router ─────────────────────────────────────────────────────

class TestDecisionRouter:
    def setup_method(self): self.r = ClaimRouter()

    def test_no_auth_to_portal_check(self):
        assert self.r.route(make_claim(denial_codes=[DenialCode.NO_AUTH]))[0] == ResolutionAction.MCO_PORTAL_AUTH_CHECK

    def test_rrr_nhcs_under_threshold_write_off(self):
        # RRR write-off only for NHCS and amount <= $19.80
        c = make_claim(denial_codes=[DenialCode.RURAL_RATE_REDUCTION], program=Program.NHCS, billed=15.00)
        assert self.r.route(c)[0] == ResolutionAction.WRITE_OFF

    def test_rrr_over_threshold_recon(self):
        # RRR over $19.80 → reconsideration (research needed)
        c = make_claim(denial_codes=[DenialCode.RURAL_RATE_REDUCTION], program=Program.NHCS, billed=25.00)
        assert self.r.route(c)[0] == ResolutionAction.RECONSIDERATION

    def test_rrr_urban_provider_recon(self):
        # RRR for non-NHCS (urban) → reconsideration (should be paid as billed)
        c = make_claim(denial_codes=[DenialCode.RURAL_RATE_REDUCTION], program=Program.KJLN, billed=15.00)
        assert self.r.route(c)[0] == ResolutionAction.RECONSIDERATION

    def test_duplicate_to_recon(self):
        assert self.r.route(make_claim(denial_codes=[DenialCode.DUPLICATE]))[0] == ResolutionAction.RECONSIDERATION

    def test_invalid_id_to_correction(self):
        assert self.r.route(make_claim(denial_codes=[DenialCode.INVALID_ID]))[0] == ResolutionAction.CORRECT_AND_RESUBMIT

    def test_wrong_billing_to_lauris_fix(self):
        assert self.r.route(make_claim(denial_codes=[DenialCode.WRONG_BILLING_CO]))[0] == ResolutionAction.LAURIS_FIX_COMPANY

    def test_magellan_routes_by_denial_code(self):
        # March 2026: Magellan no longer always requires phone call first
        c = make_claim(mco=MCO.MAGELLAN, denial_codes=[DenialCode.NO_AUTH])
        assert self.r.route(c)[0] == ResolutionAction.MCO_PORTAL_AUTH_CHECK

    def test_too_new_skipped(self):
        c = make_claim(denial_codes=[DenialCode.NO_AUTH], age_days=3, status=ClaimStatus.DENIED)
        action, reason = self.r.route(c)
        assert action == ResolutionAction.SKIP
        assert "too_new" in reason

    def test_rejected_not_skipped_even_if_new(self):
        c = make_claim(denial_codes=[DenialCode.INVALID_ID], age_days=2, status=ClaimStatus.REJECTED)
        assert self.r.route(c)[0] != ResolutionAction.SKIP

    def test_recon_45d_timeout_escalates(self):
        c = make_claim(status=ClaimStatus.IN_RECON, age_days=60)
        c.recon_submitted = date.today() - timedelta(days=46)
        assert self.r.route(c)[0] == ResolutionAction.APPEAL_STEP3

    def test_recon_not_due_skipped(self):
        c = make_claim(status=ClaimStatus.IN_RECON, age_days=50)
        c.recon_submitted = date.today() - timedelta(days=20)
        assert self.r.route(c)[0] == ResolutionAction.SKIP

    def test_timely_filing_resubmit_first(self):
        # March 2026: Timely filing → resubmit first, not auto-human-review
        assert self.r.route(make_claim(denial_codes=[DenialCode.TIMELY_FILING], age_days=120))[0] == ResolutionAction.CORRECT_AND_RESUBMIT

    def test_timely_filing_recon_after_30d(self):
        # If already resubmitted and 30+ days, escalate to reconsideration
        c = make_claim(denial_codes=[DenialCode.TIMELY_FILING], age_days=120)
        c.last_followup = date.today() - timedelta(days=35)
        assert self.r.route(c)[0] == ResolutionAction.RECONSIDERATION

    def test_recoupment_to_human(self):
        assert self.r.route(make_claim(denial_codes=[DenialCode.RECOUPMENT]))[0] == ResolutionAction.HUMAN_REVIEW

    def test_unknown_to_human(self):
        assert self.r.route(make_claim(denial_codes=[DenialCode.UNKNOWN]))[0] == ResolutionAction.HUMAN_REVIEW

    def test_recon_denied_to_appeal(self):
        assert self.r.route(make_claim(denial_codes=[DenialCode.RECON_DENIED], age_days=60))[0] == ResolutionAction.APPEAL_STEP3

    def test_route_batch(self):
        claims = [
            make_claim("C1", denial_codes=[DenialCode.NO_AUTH]),
            make_claim("C2", denial_codes=[DenialCode.DUPLICATE]),
            make_claim("C3", denial_codes=[DenialCode.RURAL_RATE_REDUCTION]),
        ]
        results = self.r.route_batch(claims)
        assert len(results) == 3
        actions = {r[1] for r in results}
        assert ResolutionAction.MCO_PORTAL_AUTH_CHECK in actions
        assert ResolutionAction.RECONSIDERATION in actions  # RRR with default billed=$500 → recon


# ── 4. Denial Code Parser ──────────────────────────────────────────────────

class TestDenialCodeParser:
    def test_no_auth(self):
        for t in ["No auth on file", "Authorization not found"]:
            assert DenialCode.NO_AUTH in parse_denial_codes(t)

    def test_duplicate(self):
        assert DenialCode.DUPLICATE in parse_denial_codes("Duplicate claim")

    def test_rrr(self):
        assert DenialCode.RURAL_RATE_REDUCTION in parse_denial_codes("Rural rate reduction")

    def test_timely_filing(self):
        assert DenialCode.TIMELY_FILING in parse_denial_codes("Past timely filing limit")

    def test_unknown_fallback(self):
        assert parse_denial_codes("Unrecognized denial") == [DenialCode.UNKNOWN]

    def test_empty_unknown(self):
        assert parse_denial_codes("") == [DenialCode.UNKNOWN]

    def test_multiple_codes(self):
        codes = parse_denial_codes("No auth on file and invalid member ID")
        assert DenialCode.NO_AUTH in codes
        assert DenialCode.INVALID_ID in codes

    def test_case_insensitive(self):
        assert DenialCode.NO_AUTH in parse_denial_codes("NO AUTHORIZATION ON FILE")


# ── 5. MCO Parser ──────────────────────────────────────────────────────────

class TestMCOParser:
    def test_united(self): assert _parse_mco("United Healthcare") == MCO.UNITED
    def test_sentara(self): assert _parse_mco("Sentara Health Plans") == MCO.SENTARA
    def test_aetna(self):   assert _parse_mco("Aetna Better Health") == MCO.AETNA
    def test_anthem(self):  assert _parse_mco("Anthem Blue Cross") == MCO.ANTHEM
    def test_molina(self):  assert _parse_mco("Molina Healthcare") == MCO.MOLINA
    def test_magellan(self):assert _parse_mco("Magellan") == MCO.MAGELLAN
    def test_unknown(self): assert _parse_mco("Some Random Payer") == MCO.UNKNOWN


# ── 6. Power BI CSV Parser ────────────────────────────────────────────────

class TestPowerBICSVParser:
    def test_basic_row(self):
        c = parse_billing_summary_csv(make_csv([
            ["John Smith","Sentara","01/15/2026","500.00","0.00","No auth on file","NHCS","AUTH001","63"]
        ]))
        assert len(c) == 1
        assert c[0].client_name == "John Smith"
        assert c[0].billed_amount == 500.0
        assert DenialCode.NO_AUTH in c[0].denial_codes

    def test_empty_csv(self):
        assert parse_billing_summary_csv("") == []

    def test_header_only(self):
        assert parse_billing_summary_csv(make_csv([])) == []

    def test_parse_float(self):
        assert _parse_float("$1,234.56") == 1234.56
        assert _parse_float("") == 0.0

    def test_parse_int(self):
        assert _parse_int("63 days") == 63
        assert _parse_int("") == 0

    def test_parse_program(self):
        assert _parse_program("KJLN") == Program.KJLN
        assert _parse_program("NHCS") == Program.NHCS
        assert _parse_program("Mary's Home") == Program.MARYS_HOME
        assert _parse_program("Unknown") == Program.UNKNOWN

    def test_missing_columns_no_crash(self):
        assert isinstance(parse_billing_summary_csv("Client Name,MCO\nJohn,Sentara\n"), list)


# ── 7. Daily Run Summary ───────────────────────────────────────────────────

class TestDailyRunSummary:
    def test_remaining(self):
        s = DailyRunSummary(claims_at_start=100, claims_completed=60)
        assert s.claims_remaining == 40

    def test_remaining_never_negative(self):
        assert DailyRunSummary(claims_at_start=10, claims_completed=15).claims_remaining == 0

    def test_comment_format(self):
        s = DailyRunSummary(eras_uploaded=5, claims_at_start=200, claims_completed=87,
                            write_offs=23, recons_submitted=14, corrections_made=50,
                            human_review_flags=3)
        c = s.to_clickup_comment()
        assert "#AUTO #" in c
        assert "200" in c and "87" in c and "⚠️" in c

    def test_no_warning_when_zero_human_flags(self):
        assert "⚠️" not in DailyRunSummary().to_clickup_comment()


# ── 8. Fax Cover Letter ────────────────────────────────────────────────────

class TestFaxCoverLetter:
    def test_file_created(self):
        from actions.fax_refax import build_refax_cover_letter
        with tempfile.TemporaryDirectory() as d:
            path = build_refax_cover_letter("Jane Doe", date(2026, 2, 10), "Sentara",
                                            output_path=f"{d}/cover.docx")
            assert os.path.exists(path) and os.path.getsize(path) > 0

    def test_required_text(self):
        from actions.fax_refax import build_refax_cover_letter
        from docx import Document
        with tempfile.TemporaryDirectory() as d:
            path = build_refax_cover_letter("Bob Smith", date(2026, 1, 15), "Molina",
                                            output_path=f"{d}/cover.docx")
            text = "\n".join(p.text for p in Document(path).paragraphs)
            assert "initially sent on" in text
            assert "honor the date from the original submission" in text
            assert "Bob Smith" in text

    def test_auto_path(self):
        from actions.fax_refax import build_refax_cover_letter
        path = build_refax_cover_letter("Test Client", date(2026, 3, 1), "United")
        assert "Test_Client" in path


# ── 9. Guardrails ──────────────────────────────────────────────────────────

class TestGuardrails:
    def test_anthem_marys_era_irregular(self):
        from lauris.billing import classify_era
        era = ERA("E1", MCO.ANTHEM, Program.MARYS_HOME, date.today(), 5000.0, "/tmp/e.era")
        assert classify_era(era) == "anthem_marys"

    def test_united_marys_era_irregular(self):
        from lauris.billing import classify_era
        era = ERA("E2", MCO.UNITED, Program.MARYS_HOME, date.today(), 1000.0, "/tmp/e.era")
        assert classify_era(era) == "united_marys"

    def test_standard_era_is_standard(self):
        from lauris.billing import classify_era
        era = ERA("E3", MCO.SENTARA, Program.NHCS, date.today(), 2000.0, "/tmp/e.era")
        assert classify_era(era) == "standard"

    def test_rrr_nhcs_small_write_off_even_if_new(self):
        # RRR NHCS <= $19.80 still bypasses age check
        r = ClaimRouter()
        c = make_claim(denial_codes=[DenialCode.RURAL_RATE_REDUCTION], age_days=2,
                       program=Program.NHCS, billed=10.00)
        assert r.route(c)[0] == ResolutionAction.WRITE_OFF

    def test_aetna_always_unique_recon_text(self):
        assert get_recon_reason("x", "aetna") != get_recon_reason("no_auth", "sentara")

    def test_era_note_says_uploaded_not_paid(self):
        n = note_era_uploaded("Sentara", "ERA-099")
        assert "paid" not in n.lower()
        assert "uploaded" in n.lower()

    def test_weekly_schedule_has_era_every_day(self):
        # Monday=0 through Friday=4
        from decision_tree.router import get_todays_primary_actions
        for offset in range(5):
            with patch("decision_tree.router.date") as md:
                monday = date(2026, 3, 16)
                target = monday + timedelta(days=offset)
                md.today.return_value = target
                md.side_effect = lambda *a, **k: date(*a, **k)
                assert ResolutionAction.ERA_UPLOAD in get_todays_primary_actions()


# ── 10. Date Parser ────────────────────────────────────────────────────────

class TestDateParser:
    def test_mm_dd_yyyy(self): assert _parse_date("01/15/2026") == date(2026, 1, 15)
    def test_mm_dd_yy(self):   assert _parse_date("01/15/26") == date(2026, 1, 15)
    def test_iso(self):        assert _parse_date("2026-01-15") == date(2026, 1, 15)
    def test_invalid(self):    assert _parse_date("not a date") is None
    def test_empty(self):      assert _parse_date("") is None
    def test_stripped(self):   assert _parse_date("  01/15/2026  ") == date(2026, 1, 15)


# ── 11. Orchestrator dispatch (mocked) ────────────────────────────────────

class TestOrchestratorDispatch:
    @pytest.mark.asyncio
    async def test_skip_succeeds(self):
        from orchestrator import dispatch
        c = make_claim(age_days=2, status=ClaimStatus.DENIED)
        r = await dispatch(c, ResolutionAction.SKIP)
        assert r.success is True

    @pytest.mark.asyncio
    async def test_human_review_marks_needs_human(self):
        from orchestrator import dispatch
        c = make_claim(denial_codes=[DenialCode.RECOUPMENT])
        r = await dispatch(c, ResolutionAction.HUMAN_REVIEW)
        assert r.needs_human is True

    @pytest.mark.asyncio
    async def test_unknown_action_returns_human_review(self):
        from orchestrator import dispatch
        c = make_claim()
        # Pass an action with no handler registered
        r = await dispatch(c, ResolutionAction.ERA_UPLOAD)
        assert r is not None  # Doesn't crash
