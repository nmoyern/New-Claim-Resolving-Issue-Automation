"""Availity 276/277 claim-status bucketing for outstanding claims.

Walks `docs/outstanding_claims_unified.xlsx`, filters to Availity-supported MCOs
(Sentara 54154, UHC 87726, Humana 61101, Aetna ABHVA), joins to the Lauris DOB
view for patient demographics, submits a 276 per claim, polls for the 277, and
classifies each claim into one of five buckets:

    A  already_paid_era_gap   — payer paid; Lauris just hasn't posted the ERA
    B  real_denial            — F2 finalized/denial; pull 835 for CAS/CARC detail
    C  payer_no_record        — D0 data search unsuccessful; wrong-entity candidate
    D  too_new                — A1 received only; not yet adjudicated
    E  payer_rejected         — A3/A4/A6/A7 intake rejection; 837 construction issue

Each response is saved to `data/availity_responses/<timestamp>_<i>_<patient>.json`
for offline re-analysis without burning more API calls.

Template for building Availity-aware routing in `decision_tree/router.py`.

Usage:
    cd <project root>
    python3 tools/availity_claim_status_check.py               # default 50 claims
    N=20 python3 tools/availity_claim_status_check.py          # smaller sample
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

import pandas as pd
import requests
from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))
load_dotenv(PROJECT_ROOT / ".env")
# Also try ~/availity-test/.env for AVAILITY_PROD_* keys if they aren't in
# the project .env yet (migration path).
load_dotenv(Path.home() / "availity-test" / ".env", override=False)

REPORT = PROJECT_ROOT / "docs" / "outstanding_claims_unified.xlsx"
OUT_DIR = PROJECT_ROOT / "data" / "availity_responses"
OUT_DIR.mkdir(parents=True, exist_ok=True)

AVAILITY_BASE = "https://api.availity.com"
# Lauris custom view "Claim_DOB__x0026__Gender_AUTOMATION" — returns
# Unique_ID, Client_Name, DOB, Gender for ~3400 patients. Join key is
# Unique_ID which matches the `Unique ID` column in the unified report.
LAURIS_DEMOGRAPHICS_URL = (
    "https://www12.laurisonline.com/reports/formsearchdataviewXML.aspx"
    "?viewid=sJW17xYGLrrCB7izAXPf4Q%3d%3d"
)

N = int(os.environ.get("N", "50"))

# Availity 276 supported MCOs (verified prod 2026-04-13)
MCO_TO_PAYER = {
    "Sentara Health Plans": "54154",
    "Optima CCC+":          "54154",
    "United Health CCC+":   "87726",
    "Humana":               "61101",
    "Aetna CCC+":           "ABHVA",
}

# Lauris "Entity" → (submitter.id, NPI, providers.lastName)
ENTITY_TO_ORG = {
    "Mary's Home Inc.":              ("1636587", "1437871753", "MARYS HOME INC"),
    "New Heights Community Support": ("628128",  "1700297447", "NEW HEIGHTS COMMUNITY SUPPORT"),
    "Martinsville-NHCS":             ("628128",  "1700297447", "NEW HEIGHTS COMMUNITY SUPPORT"),
    "KJLN Inc":                      ("977164",  "1306491592", "KJLN INC"),
}

CATEGORY_LABEL = {
    "F0": "Finalized", "F1": "Paid", "F2": "Denied",
    "F3": "Revised", "F4": "Complete-no-pay",
    "A1": "Received", "A3": "Returned unprocessable",
    "A4": "Not found", "A6": "Rejected-missing info", "A7": "Rejected-invalid info",
    "D0": "Data search unsuccessful",
    "P0": "Pending", "P1": "Pending in process",
}

# Rejection patterns to exclude from the test pool (already known clearinghouse issues)
REJECTION_KEYWORDS = [
    "using group npi", "invalid npi", "rendering npi",
    "missing required", "rejected", "rejection",
    "invalid format", "bad format", "validation", "unable to process",
]


def _http(method, url, headers=None, body=None):
    import urllib.request, urllib.error
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def get_prod_token() -> str:
    cid = os.environ.get("AVAILITY_PROD_CLIENT_ID", "")
    csec = os.environ.get("AVAILITY_PROD_CLIENT_SECRET", "")
    if not (cid and csec):
        sys.exit("AVAILITY_PROD_CLIENT_ID / _CLIENT_SECRET not set in env")
    body = urlencode({
        "grant_type": "client_credentials",
        "scope": "healthcare-hipaa-transactions",
        "client_id": cid, "client_secret": csec,
    }).encode()
    status, _, raw = _http(
        "POST", f"{AVAILITY_BASE}/availity/v1/token",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        body=body,
    )
    if status != 200:
        sys.exit(f"Availity token failed: {status}\n{raw.decode(errors='replace')}")
    return json.loads(raw)["access_token"]


def fetch_lauris_demographics() -> dict:
    """{Unique_ID: {first, last, dob, gender_code}} from the
    Claim_DOB__x0026__Gender_AUTOMATION view. Gender comes back as the
    strings 'Male'/'Female' which we map to the Availity 276 codes 'M'/'F'."""
    r = requests.get(
        LAURIS_DEMOGRAPHICS_URL,
        auth=(os.environ["LAURIS_USERNAME"], os.environ["LAURIS_PASSWORD"]),
        timeout=300,
    )
    r.raise_for_status()
    root = ET.fromstring(r.text)
    out = {}
    for row in root.findall("Claim_DOB__x0026__Gender_AUTOMATION"):
        uid = (row.findtext("Unique_ID") or "").strip()
        dob = (row.findtext("DOB") or "").strip()[:10]
        if not (uid and dob):
            continue
        full_name = (row.findtext("Client_Name") or "").strip()
        # Client_Name is "First Last" or "First Middle Last"
        parts = full_name.split()
        first = parts[0].upper() if parts else ""
        last = parts[-1].upper() if len(parts) >= 2 else ""
        gender_raw = (row.findtext("Gender") or "").strip().lower()
        gender_code = "F" if gender_raw.startswith("f") else (
            "M" if gender_raw.startswith("m") else "U"
        )
        out[uid] = {
            "first": first,
            "last":  last,
            "dob":   dob,
            "gender_code": gender_code,
            "full_name": full_name,
        }
    return out


def is_rejection(denial_text: str) -> bool:
    if not isinstance(denial_text, str):
        return False
    t = denial_text.lower()
    return any(kw in t for kw in REJECTION_KEYWORDS)


def submit_and_poll(token: str, payload: dict, max_wait: int = 45) -> dict:
    s, _, r = _http(
        "POST", f"{AVAILITY_BASE}/availity/v1/claim-statuses",
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-HTTP-Method-Override": "GET",
            "Accept": "application/json",
        },
        body=urlencode(payload).encode(),
    )
    if s not in (200, 202):
        try:
            return {"_http": s, "_error": json.loads(r)}
        except Exception:  # noqa: BLE001
            return {"_http": s, "_error": r.decode(errors="replace")[:500]}
    try:
        href = json.loads(r)["claimStatuses"][0]["links"]["self"]["href"]
    except Exception:  # noqa: BLE001
        return {"_http": s, "_error": "no href"}
    deadline = time.time() + max_wait
    while time.time() < deadline:
        time.sleep(2)
        ps, _, pr = _http(
            "GET", href,
            headers={"Authorization": f"Bearer {token}", "Accept": "application/json"},
        )
        if ps == 200:
            return json.loads(pr)
        if ps != 202:
            try:
                return {"_http": ps, "_error": json.loads(pr)}
            except Exception:  # noqa: BLE001
                return {"_http": ps, "_error": pr.decode(errors="replace")[:500]}
    return {"_http": "timeout"}


def classify(resp: dict) -> dict:
    """Parse a 277 response and assign a bucket."""
    if not resp or resp.get("_error") or resp.get("_http"):
        return {"ok": False, "http": resp.get("_http") if resp else "?",
                "error": str(resp.get("_error", ""))[:200] if resp else "no-response"}
    css = resp.get("claimStatuses") or []
    records = []
    paid_total = 0.0
    for cs in css:
        for sd in cs.get("statusDetails") or []:
            try:
                paid = float(sd.get("paymentAmount", "0") or 0)
            except ValueError:
                paid = 0.0
            paid_total += paid
            records.append({
                "control": cs.get("claimControlNumber", "") or cs.get("traceId", ""),
                "categoryCode": sd.get("categoryCode", ""),
                "billed": sd.get("claimAmount", ""),
                "paid": paid,
                "fin": sd.get("finalizedDate", ""),
                "era": sd.get("remittanceDate", ""),
            })
    cat_counts = {}
    for r in records:
        c = r["categoryCode"] or "?"
        cat_counts[c] = cat_counts.get(c, 0) + 1
    any_paid = any(r["paid"] > 0 for r in records)
    all_d0 = bool(records) and all(r["categoryCode"] == "D0" for r in records)
    only_a1 = set(cat_counts.keys()) == {"A1"}
    any_a_reject = bool({"A3", "A4", "A6", "A7"} & set(cat_counts.keys()))
    if all_d0:
        bucket = "C_payer_no_record"
    elif any_a_reject:
        bucket = "E_payer_rejected"
    elif only_a1 and not any_paid:
        bucket = "D_too_new"
    elif any_paid:
        bucket = "A_paid_era_gap"
    elif "F2" in cat_counts:
        bucket = "B_real_denial"
    else:
        bucket = "F_other"
    return {
        "ok": True, "bucket": bucket, "adj_count": len(records),
        "categories": cat_counts, "paid_total": paid_total,
        "records": records,
    }


def pick_eligible(df, demo, n, seen_uids=None):
    picks = []
    seen_uids = seen_uids or set()
    for _, row in df.iterrows():
        if len(picks) >= n:
            break
        mco = str(row.get("MCO", "")).strip()
        if mco not in MCO_TO_PAYER:
            continue
        if is_rejection(row.get("Denial Reason", "")):
            continue
        if is_rejection(row.get("Claim.MD Notes", "")):
            continue
        member = str(row.get("Member ID", "")).strip()
        if not member or member.lower() in ("none reported", "nan"):
            continue
        dos = pd.to_datetime(row.get("DOS"), errors="coerce")
        if pd.isna(dos):
            continue
        uid = str(row.get("Unique ID", "")).strip()
        info = demo.get(uid)
        if not info:
            continue
        full_name = f"{info['first']} {info['last']}".upper()
        if full_name in seen_uids:
            continue
        picks.append({"row": row, "info": info, "dos": dos, "mco": mco})
        seen_uids.add(full_name)
    return picks


async def main():
    print(f"Availity 276 bucketing — up to {N} claims")
    token = get_prod_token()
    demo = fetch_lauris_demographics()
    print(f"  Lauris DOB: {len(demo)} patients")
    df = pd.read_excel(REPORT)
    print(f"  report rows: {len(df)}")

    picks = pick_eligible(df, demo, n=N)
    print(f"  {len(picks)} claims selected after filters")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    results = []
    for i, pick in enumerate(picks, 1):
        row, info, dos, mco = pick["row"], pick["info"], pick["dos"], pick["mco"]
        payer_id = MCO_TO_PAYER[mco]
        entity = str(row.get("Entity", ""))
        sub_id, our_npi, prov_last = ENTITY_TO_ORG.get(entity, ("1209588", "", entity.upper()[:35]))
        dos_str = dos.strftime("%Y-%m-%d")

        payload = {
            "payer.id":                           payer_id,
            "submitter.lastName":                 "LIFECONSULTANTS",
            "submitter.id":                       sub_id,
            "providers.lastName":                 prov_last,
            "providers.npi":                      our_npi,
            "subscriber.memberId":                str(row["Member ID"]).strip(),
            "subscriber.lastName":                info["last"],
            "subscriber.firstName":               info["first"],
            "patient.lastName":                   info["last"],
            "patient.firstName":                  info["first"],
            "patient.birthDate":                  info["dob"],
            "patient.genderCode":                 info.get("gender_code", "M"),
            "patient.subscriberRelationshipCode": "18",
            "fromDate":                           dos_str,
            "toDate":                             dos_str,
        }

        print(f"\n[{i:3}/{len(picks)}] {info['first']} {info['last']} | {mco} | "
              f"DOS {dos_str} | ${row['Outstanding']:.2f}")

        resp = submit_and_poll(token, payload)
        (OUT_DIR / f"{timestamp}_{i:03d}_{info['last']}_{dos_str}.json").write_text(
            json.dumps({"payload": payload, "response": resp}, indent=2, default=str)
        )

        summary = classify(resp)
        if not summary["ok"]:
            print(f"        ERROR {summary.get('http','?')}: {str(summary.get('error',''))[:120]}")
        else:
            cats = summary["categories"]
            cat_str = ", ".join(
                f"{k}={v}({CATEGORY_LABEL.get(k, '?')})" for k, v in cats.items()
            ) or "no records"
            print(f"        [{summary['bucket']}] paid ${summary['paid_total']:.2f} | {cat_str}")
        results.append({"pick": pick, "summary": summary})
        time.sleep(0.5)

    # Rollup
    print("\n" + "=" * 90)
    print("\n📊 ROLLUP\n")
    buckets = {}
    dollars = {}
    for r in results:
        s = r["summary"]
        b = s.get("bucket", "error") if s.get("ok") else "error"
        buckets[b] = buckets.get(b, 0) + 1
        dollars[b] = dollars.get(b, 0) + float(r["pick"]["row"]["Outstanding"])
    labels = {
        "A_paid_era_gap":    "Paid at payer, ERA not posted (auto-clears when ERAs post)",
        "B_real_denial":     "Real denial — pull ERA, route through decision tree",
        "C_payer_no_record": "Payer has no record (wrong-entity candidate)",
        "D_too_new":         "Too new — wait and recheck",
        "E_payer_rejected":  "Payer-rejected at intake (837 structural issue)",
        "F_other":           "Other / unclassified",
        "error":             "API error",
    }
    for b in ["A_paid_era_gap", "B_real_denial", "C_payer_no_record",
              "D_too_new", "E_payer_rejected", "F_other", "error"]:
        if b in buckets:
            print(f"  {buckets[b]:>3}  ${dollars.get(b, 0):>11,.2f}  {labels[b]}")
    agg_path = OUT_DIR / f"{timestamp}_rollup.json"
    agg_path.write_text(json.dumps(
        [{"row_member": str(r["pick"]["row"]["Member ID"]),
          "row_dos": str(r["pick"]["row"]["DOS"]),
          "summary": r["summary"]}
         for r in results],
        indent=2, default=str,
    ))
    print(f"\nSaved: {agg_path}")


if __name__ == "__main__":
    asyncio.run(main())
