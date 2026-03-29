"""
tests/conftest.py
-----------------
Shared pytest fixtures and environment configuration.
Runs before any test module is imported.
"""
import os
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

# ── Project root on path ──────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent.parent))

# ── Force dry-run and test initials before any module loads ───────────────
os.environ.setdefault("DRY_RUN", "true")
os.environ.setdefault("AUTOMATION_INITIALS", "TEST")
os.environ.setdefault("LOG_DIR", "/tmp/claims_test_logs")
os.environ.setdefault("SESSION_DIR", "/tmp/claims_test_sessions")
os.environ.setdefault("SKIP_CLAIMS_NEWER_THAN_DAYS", "7")


# ── Shared claim factory ───────────────────────────────────────────────────

@pytest.fixture
def make_claim():
    """Factory fixture: returns a callable that builds test Claims."""
    from config.models import Claim, ClaimStatus, DenialCode, MCO, Program

    def _factory(
        claim_id="C001",
        client_name="John Doe",
        client_id="MBR001",
        mco=MCO.SENTARA,
        program=Program.NHCS,
        denial_codes=None,
        status=ClaimStatus.DENIED,
        age_days=30,
        billed=500.0,
        paid=0.0,
        billing_region="NHCS",
    ) -> Claim:
        return Claim(
            claim_id=claim_id,
            client_name=client_name,
            client_id=client_id,
            dos=date(2026, 1, 15),
            mco=mco,
            program=program,
            billed_amount=billed,
            paid_amount=paid,
            status=status,
            denial_codes=denial_codes if denial_codes is not None else [DenialCode.NO_AUTH],
            age_days=age_days,
            billing_region=billing_region,
        )

    return _factory


@pytest.fixture
def make_era():
    """Factory fixture: returns a callable that builds test ERAs."""
    from config.models import ERA, MCO, Program

    def _factory(
        era_id="ERA001",
        mco=MCO.SENTARA,
        program=Program.NHCS,
        total=1000.0,
        file_path="/tmp/test.835",
    ) -> ERA:
        return ERA(
            era_id=era_id,
            mco=mco,
            program=program,
            payment_date=date.today(),
            total_amount=total,
            file_path=file_path,
        )

    return _factory


@pytest.fixture
def mock_claimmd(mocker):
    """Pre-wired AsyncMock for ClaimMDSession."""
    from unittest.mock import AsyncMock
    m = AsyncMock()
    m.__aenter__ = AsyncMock(return_value=m)
    m.__aexit__ = AsyncMock(return_value=False)
    m.get_denied_claims = AsyncMock(return_value=[])
    m.download_eras = AsyncMock(return_value=[])
    m.correct_and_resubmit = AsyncMock(return_value=True)
    m.submit_reconsideration = AsyncMock(return_value=True)
    m.submit_appeal = AsyncMock(return_value=True)
    m.write_claimmd_writeoff_note = AsyncMock(return_value=True)
    m._open_claim = AsyncMock(return_value=True)
    m._write_note_no_save = AsyncMock()
    return m


@pytest.fixture
def mock_lauris(mocker):
    """Pre-wired AsyncMock for LaurisSession."""
    from unittest.mock import AsyncMock
    m = AsyncMock()
    m.__aenter__ = AsyncMock(return_value=m)
    m.__aexit__ = AsyncMock(return_value=False)
    m.upload_era = AsyncMock(return_value=True)
    m.write_off_claim = AsyncMock(return_value=True)
    m.fix_billing_company = AsyncMock(return_value=True)
    m.add_authorization = AsyncMock(return_value=True)
    m.check_fax_status = AsyncMock(return_value=(True, date(2026, 1, 10), "FAX001"))
    m.get_fax_confirmation_screenshot = AsyncMock(return_value=True)
    m.resend_failed_fax = AsyncMock(return_value=True)
    m._navigate_to_billing_center = AsyncMock()
    m._navigate_to_client = AsyncMock()
    m._navigate_to_fax_proxy = AsyncMock()
    m.page = AsyncMock()
    return m


@pytest.fixture
def mock_clickup(mocker):
    """Pre-wired AsyncMock for ClickUpLogger."""
    from unittest.mock import AsyncMock
    m = AsyncMock()
    m.post_comment = AsyncMock(return_value=True)
    m.post_human_review_alert = AsyncMock(return_value=True)
    return m


@pytest.fixture
def tmp_work_dir(tmp_path):
    """A temporary work directory for fax/doc creation tests."""
    d = tmp_path / "claims_work"
    d.mkdir()
    return d


@pytest.fixture(autouse=True)
def ensure_log_dirs():
    """Make sure log directories exist before each test."""
    Path("/tmp/claims_test_logs").mkdir(parents=True, exist_ok=True)
    Path("/tmp/claims_test_sessions").mkdir(parents=True, exist_ok=True)
    yield
