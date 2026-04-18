from datetime import date

from config.models import Claim, ClaimStatus, MCO, Program
from sources.lauris_demographics import (
    LaurisDemographics,
    enrich_claim_with_demographics,
    find_demographics_for_claim,
)
import sources.lauris_demographics as lauris_demographics


def _claim(name="Jane Doe", lauris_id="ID001"):
    return Claim(
        claim_id="C123",
        client_name=name,
        client_id="M123",
        dos=date(2026, 1, 15),
        mco=MCO.AETNA,
        program=Program.NHCS,
        billed_amount=100.0,
        status=ClaimStatus.DENIED,
        lauris_id=lauris_id,
    )


def _demo(uid="ID001", name="Jane Doe", dob="2000-01-02", gender="F"):
    return LaurisDemographics(
        lauris_id=uid,
        full_name=name,
        first=(name.split(",", 1)[1].strip().split()[0].upper() if "," in name else name.split()[0].upper()),
        last=(name.split(",", 1)[0].strip().upper() if "," in name else name.split()[-1].upper()),
        dob=dob,
        gender_code=gender,
    )


def test_finds_demographics_by_lauris_id_first():
    demographics = {
        "ID001": _demo(name="Different Name"),
        "ID002": _demo(uid="ID002", name="Jane Doe"),
    }

    result = find_demographics_for_claim(_claim(name="Jane Doe", lauris_id="ID001"), demographics)

    assert result.lauris_id == "ID001"


def test_finds_demographics_by_first_and_last_when_no_lauris_id():
    demographics = {
        "ID002": _demo(uid="ID002", name="Jane Marie Doe"),
    }

    result = find_demographics_for_claim(_claim(name="Jane Doe", lauris_id=""), demographics)

    assert result.lauris_id == "ID002"


def test_enrich_claim_attaches_dynamic_dob_and_gender():
    claim = _claim()

    enriched = enrich_claim_with_demographics(claim, {"ID001": _demo()})

    assert enriched is claim
    assert claim.client_dob == "2000-01-02"
    assert claim.gender_code == "F"
    assert claim.patient_full_name == "Jane Doe"
    assert claim.patient_first_name == "JANE"
    assert claim.patient_last_name == "DOE"
    assert claim.lauris_id == "ID001"


def test_splits_lauris_comma_name_as_first_and_last():
    demo = _demo(name="PERRY, SHANE")

    assert demo.first == "SHANE"
    assert demo.last == "PERRY"


def test_uses_member_id_and_dos_bridge_before_name_matching(monkeypatch):
    claim = _claim(name="CW4178-1176811", lauris_id="")
    claim.client_id = "975006024638"

    monkeypatch.setattr(
        lauris_demographics,
        "_billing_bridge_lookup",
        lambda: {
            ("975006024638", "2026-01-15"): {
                "bs_id": "BS123",
                "member_id": "975006024638",
                "doc_date": "2026-01-15",
                "key": "ID_GOOD",
                "name": "SHAW, JOSHUA",
                "auth_id": "AUTH123",
                "auth_number": "UM95843365",
            }
        },
    )
    demographics = {
        "ID_BAD": _demo(uid="ID_BAD", name="test"),
        "ID_GOOD": _demo(uid="ID_GOOD", name="SHAW, JOSHUA"),
    }

    result = find_demographics_for_claim(claim, demographics)

    assert result is not None
    assert result.lauris_id == "ID_GOOD"
    assert result.full_name == "SHAW, JOSHUA"
    assert claim.auth_number == "UM95843365"
    assert claim.patient_full_name == "SHAW, JOSHUA"
    assert claim.patient_first_name == "JOSHUA"
    assert claim.patient_last_name == "SHAW"


def test_returns_none_when_member_id_bridge_has_no_match(monkeypatch):
    claim = _claim(name="CW4178-1176811", lauris_id="")
    claim.client_id = "975006024638"

    monkeypatch.setattr(
        lauris_demographics,
        "_billing_bridge_lookup",
        lambda: {("975006024638", "2026-01-14"): {"key": "ID_OTHER"}},
    )

    result = find_demographics_for_claim(claim, {"ID_OTHER": _demo(uid="ID_OTHER", name="SMITH, JOHN")})

    assert result is None
