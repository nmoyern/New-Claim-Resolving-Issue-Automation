#!/usr/bin/env python3
"""
Quick test: verify the eligibility DOB fallback + Availity claim status
works for three sample Sentara claims whose patients are missing from
the Lauris DOB/Gender view.
"""
import asyncio
import os
import sys

# Load .env from project root
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / ".env")

# Also pull Availity prod creds from ~/availity-test/.env if not already set
if not os.environ.get("AVAILITY_PROD_CLIENT_ID"):
    avail_env = Path.home() / "availity-test" / ".env"
    if avail_env.exists():
        load_dotenv(avail_env, override=False)
        print(f"[info] Loaded Availity API creds from {avail_env}")

from datetime import date
from config.models import Claim, ClaimStatus, DenialCode, MCO, Program
from sources.lauris_demographics import (
    enrich_claim_with_demographics,
    fetch_lauris_demographics,
    find_demographics_for_claim,
)
from sources.payer_inquiry import (
    ensure_claim_patient_identity,
    AvailityClaimStatusClient,
    _claim_dob,
    _claim_name_parts,
)
from actions.company_auth_match import classify_with_payer_lookup

SAMPLE_CLAIMS = [
    {
        "claim_id": "721878309",
        "client_name": "CW4178-1176811",
        "client_id": "975006024638",
        "dos": date(2025, 10, 20),
        "mco": MCO.SENTARA,
        "program": Program.NHCS,
        "claimmd_payer_id": "54154",
        "expected_lauris_id": "ID004086",
        "expected_auth": "250908941",
    },
    {
        "claim_id": "721878312",
        "client_name": "CW4178-1179243",
        "client_id": "975006024638",
        "dos": date(2025, 10, 23),
        "mco": MCO.SENTARA,
        "program": Program.NHCS,
        "claimmd_payer_id": "54154",
        "expected_lauris_id": "ID004086",
        "expected_auth": "250908941",
    },
    {
        "claim_id": "721878178",
        "client_name": "CW4178-1181497",
        "client_id": "710319037010",
        "dos": date(2025, 10, 26),
        "mco": MCO.SENTARA,
        "program": Program.NHCS,
        "claimmd_payer_id": "54154",
        "expected_lauris_id": "ID004639",
        "expected_auth": "Denied",
    },
]


async def test_one(info: dict) -> None:
    claim = Claim(
        claim_id=info["claim_id"],
        client_name=info["client_name"],
        client_id=info["client_id"],
        dos=info["dos"],
        mco=info["mco"],
        program=info["program"],
        billed_amount=100.0,
        status=ClaimStatus.DENIED,
        denial_codes=[DenialCode.UNKNOWN],
        npi="1700297447",  # NHCS
    )
    claim.claimmd_payer_id = info["claimmd_payer_id"]

    print(f"\n{'='*60}")
    print(f"Claim {claim.claim_id}  member={claim.client_id}  DOS={claim.dos}")
    print(f"{'='*60}")

    # Step 1: Try Lauris demographics (billing bridge)
    print("\n--- Step 1: Lauris demographics enrichment ---")
    enrich_claim_with_demographics(claim)
    print(f"  lauris_id:    {claim.lauris_id or '(none)'}")
    print(f"  auth_number:  {getattr(claim, 'auth_number', '') or '(none)'}")
    print(f"  patient_name: {getattr(claim, 'patient_full_name', '') or '(none)'}")
    print(f"  DOB:          {_claim_dob(claim) or '(none)'}")
    print(f"  gender:       {getattr(claim, 'gender_code', '') or '(none)'}")

    # Step 2: Check if DOB/Gender view had this patient
    demographics = fetch_lauris_demographics()
    in_dob_view = claim.lauris_id in demographics if claim.lauris_id else False
    print(f"  In DOB/Gender view: {in_dob_view}")

    # Step 3: Try eligibility fallback (THE FIX)
    if not _claim_dob(claim):
        print("\n--- Step 2: Eligibility DOB fallback ---")
        first, last = _claim_name_parts(claim)
        print(f"  Using name: {first} {last}")
        print(f"  Using payer_id: {claim.claimmd_payer_id}")
        await ensure_claim_patient_identity(claim)
        dob = _claim_dob(claim)
        print(f"  DOB after fallback: {dob or 'STILL MISSING'}")
    else:
        print(f"\n--- Step 2: DOB already available, skipping fallback ---")

    # Step 4: Attempt Availity claim status
    dob = _claim_dob(claim)
    first, last = _claim_name_parts(claim)
    print(f"\n--- Step 3: Availity claim status ---")
    print(f"  Sending: name={first} {last}, DOB={dob}, member={claim.client_id}")

    if not dob:
        print(f"  BLOCKED: Still no DOB — Availity call would fail")
        return

    if not os.environ.get("AVAILITY_PROD_CLIENT_ID"):
        print(f"  BLOCKED: AVAILITY_PROD_CLIENT_ID not set")
        return

    client = AvailityClaimStatusClient()
    result = await client.check_claim(claim)
    print(f"  gateway:        {result.gateway}")
    print(f"  bucket:         {result.bucket}")
    print(f"  ok:             {result.ok}")
    print(f"  should_process: {result.should_process}")
    print(f"  reason:         {result.reason[:150]}")
    if result.detail_summary:
        print(f"  detail:         {result.detail_summary[:120]}")

    # Step 4: Entity/auth classification
    if result.should_process:
        print(f"\n--- Step 4: Entity/auth classification ---")
        try:
            auth_match = await classify_with_payer_lookup(claim)
            print(f"  status:         {auth_match.status}")
            print(f"  action:         {auth_match.recommended_action}")
            print(f"  reason:         {auth_match.reason[:150]}")
            if auth_match.fields_to_change:
                print(f"  fields_to_change: {auth_match.fields_to_change}")
            if auth_match.should_update_claim:
                print(f"  >>> WOULD REBILL under {auth_match.matched_entities[0].entity.display_name}")
            elif auth_match.needs_human:
                print(f"  >>> NEEDS HUMAN REVIEW")
        except Exception as exc:
            print(f"  ERROR: {exc}")


async def main():
    print("Availity creds:", "YES" if os.environ.get("AVAILITY_PROD_CLIENT_ID") else "NO")
    print("Lauris creds:", "YES" if os.environ.get("LAURIS_USERNAME") else "NO")
    print("ClaimMD key:", "YES" if os.environ.get("CLAIMMD_API_KEY") else "NO")

    for info in SAMPLE_CLAIMS:
        await test_one(info)

    print(f"\n{'='*60}")
    print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
