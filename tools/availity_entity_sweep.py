"""Wrong-entity detection via Availity 276 alternate-NPI sweep.

For each test patient, re-queries Availity 276 using every LCI entity NPI
*other than* the one originally billed. If any alternate entity returns real
(non-D0) claim history for the same member+DOS, that's a strong signal the
claim should have been billed under that entity all along.

This is the core primitive for updating `decision_tree/router.py` to flag
wrong-entity cases that Claim.MD cannot see on its own. Empirically ~50% of
"D0 data search unsuccessful" claims are resolvable this way (verified
2026-04-13 against 4 test patients; 2 of 4 revealed alternate-entity history).

Usage:
    cd <project root>
    python3 tools/availity_entity_sweep.py
"""

from __future__ import annotations

import asyncio
import json
import os
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
load_dotenv(Path.home() / "availity-test" / ".env", override=False)

REPORT = PROJECT_ROOT / "docs" / "outstanding_claims_unified.xlsx"
OUT_DIR = PROJECT_ROOT / "data" / "availity_responses" / "entity_sweep"
OUT_DIR.mkdir(parents=True, exist_ok=True)

AVAILITY_BASE = "https://api.availity.com"
# Lauris custom view "Claim_DOB__x0026__Gender_AUTOMATION" — patient
# demographics (Unique_ID / Client_Name / DOB / Gender) for ~3400 patients.
LAURIS_DEMOGRAPHICS_URL = (
    "https://www12.laurisonline.com/reports/formsearchdataviewXML.aspx"
    "?viewid=sJW17xYGLrrCB7izAXPf4Q%3d%3d"
)

# Every LCI entity that could legitimately bill a patient
ENTITIES = [
    # (label, submitter.id, npi, providers.lastName)
    ("Mary's Home Inc",    "1636587", "1437871753", "MARYS HOME INC"),
    ("NHCS / New Heights", "628128",  "1700297447", "NEW HEIGHTS COMMUNITY SUPPORT"),
    ("KJLN Inc",           "977164",  "1306491592", "KJLN INC"),
]

MCO_TO_PAYER = {
    "Sentara Health Plans": "54154",
    "Optima CCC+":          "54154",
    "United Health CCC+":   "87726",
    "Humana":               "61101",
    "Aetna CCC+":           "ABHVA",
}

ENTITY_TO_NPI = {
    "Mary's Home Inc.":              "1437871753",
    "New Heights Community Support": "1700297447",
    "Martinsville-NHCS":             "1700297447",
    "KJLN Inc":                      "1306491592",
}

NPI_TO_LABEL = {e[2]: e[0] for e in ENTITIES}

CATEGORY_LABEL = {
    "F0": "Finalized", "F1": "Paid", "F2": "Denied",
    "F3": "Revised", "F4": "Complete-no-pay",
    "A1": "Received", "D0": "Data search unsuccessful",
}

# Default test set: first 4 D0 patients from the latest outstanding report.
# Override via argv: python3 availity_entity_sweep.py "NAME|MCO|YYYY-MM-DD" ...
DEFAULT_CASES = [
    ("ANGELO SMITH",       "United Health CCC+", "2026-01-03"),
    ("DONILO RIMANDO",     "Aetna CCC+",         "2026-02-18"),
    ("EARL JUNIOR MCCRAW", "Aetna CCC+",         "2026-04-04"),
    ("CRAIG MOZELLE",      "Aetna CCC+",         "2025-08-22"),
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
    s, _, r = _http(
        "POST", f"{AVAILITY_BASE}/availity/v1/token",
        headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
        body=body,
    )
    if s != 200:
        sys.exit(f"Availity token failed: {s}")
    return json.loads(r)["access_token"]


def fetch_lauris_demographics() -> dict:
    """{Unique_ID: {first, last, dob, gender_code}} from the
    Claim_DOB__x0026__Gender_AUTOMATION view."""
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
        }
    return out


def submit_and_poll(token, payload, max_wait=45):
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


def summarize(resp: dict) -> dict:
    if not resp or resp.get("_error") or resp.get("_http"):
        return {"ok": False, "http": resp.get("_http") if resp else "?",
                "error": str(resp.get("_error", ""))[:200] if resp else "no-response"}
    css = resp.get("claimStatuses") or []
    records = []
    paid_total = 0.0
    for cs in css:
        for sd in cs.get("statusDetails") or []:
            records.append({
                "control": cs.get("claimControlNumber", "") or cs.get("traceId", ""),
                "categoryCode": sd.get("categoryCode", ""),
                "billed": sd.get("claimAmount", ""),
                "paid": sd.get("paymentAmount", ""),
                "fin": sd.get("finalizedDate", ""),
            })
            try:
                paid_total += float(sd.get("paymentAmount", "0") or 0)
            except ValueError:
                pass
    cat_counts = {}
    for r in records:
        c = r["categoryCode"] or "?"
        cat_counts[c] = cat_counts.get(c, 0) + 1
    is_d0_only = bool(records) and all(r["categoryCode"] == "D0" for r in records)
    return {
        "ok": True,
        "adjudication_count": len(records),
        "categories": cat_counts,
        "paid_total": paid_total,
        "records": records,
        "d0_only": is_d0_only,
        "meaningful": bool(records) and not is_d0_only,
    }


async def main():
    token = get_prod_token()
    print(f"Token ok ({len(token)} chars)\n")

    demo = fetch_lauris_demographics()
    df = pd.read_excel(REPORT)

    # Parse CLI overrides or use defaults
    cases_input = []
    for arg in sys.argv[1:]:
        parts = arg.split("|")
        if len(parts) == 3:
            cases_input.append((parts[0].upper(), parts[1], parts[2]))
    if not cases_input:
        cases_input = DEFAULT_CASES

    cases = []
    for name, mco, dos in cases_input:
        last = name.split()[-1]
        matches = df[
            (df["MCO"] == mco)
            & df["Client Name"].str.upper().str.contains(last)
            & (df["DOS"].astype(str).str[:10] == dos)
        ]
        if matches.empty:
            print(f"  ⚠️ no match for {name} / {mco} / {dos}")
            continue
        row = matches.iloc[0]
        uid = str(row.get("Unique ID", "")).strip()
        info = demo.get(uid)
        if not info:
            print(f"  ⚠️ no Lauris DOB for {name}")
            continue
        cases.append({
            "name": name, "mco": mco, "dos": dos,
            "member": str(row["Member ID"]).strip(),
            "entity": str(row["Entity"]),
            "first": info["first"], "last": info["last"], "dob": info["dob"],
            "gender_code": info.get("gender_code", "M"),
            "billed_npi": ENTITY_TO_NPI.get(str(row["Entity"]), ""),
        })

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_results = []

    print("=" * 90)
    for case in cases:
        print(f"\n🔎 {case['first']} {case['last']} | {case['mco']} | DOS {case['dos']} | "
              f"member {case['member']}")
        print(f"   originally billed under: {case['entity']} (NPI {case['billed_npi']})")

        payer_id = MCO_TO_PAYER[case["mco"]]
        case_result = {"case": case, "alternates": []}

        for label, sub_id, npi, prov_last in ENTITIES:
            is_original = (npi == case["billed_npi"])
            tag = "ORIGINAL" if is_original else "alternate"
            payload = {
                "payer.id":                           payer_id,
                "submitter.lastName":                 "LIFECONSULTANTS",
                "submitter.id":                       sub_id,
                "providers.lastName":                 prov_last,
                "providers.npi":                      npi,
                "subscriber.memberId":                case["member"],
                "subscriber.lastName":                case["last"],
                "subscriber.firstName":               case["first"],
                "patient.lastName":                   case["last"],
                "patient.firstName":                  case["first"],
                "patient.birthDate":                  case["dob"],
                "patient.genderCode":                 case.get("gender_code", "M"),
                "patient.subscriberRelationshipCode": "18",
                "fromDate":                           case["dos"],
                "toDate":                             case["dos"],
            }
            resp = submit_and_poll(token, payload)
            (OUT_DIR / f"{timestamp}_{case['last']}_{label.replace(' ', '').replace('/', '')}.json").write_text(
                json.dumps({"payload": payload, "response": resp}, indent=2, default=str)
            )
            summary = summarize(resp)
            case_result["alternates"].append({
                "label": label, "npi": npi,
                "is_original": is_original, "summary": summary,
            })

            if not summary["ok"]:
                print(f"   [{tag:>9}] {label:<22} → ERROR {summary.get('http')}: "
                      f"{summary.get('error', '')[:80]}")
                continue
            cats = summary["categories"]
            cat_str = ", ".join(
                f"{k}={v} ({CATEGORY_LABEL.get(k, '?')})" for k, v in cats.items()
            ) or "no records"
            print(f"   [{tag:>9}] {label:<22} → {summary['adjudication_count']} adj | "
                  f"paid ${summary['paid_total']:.2f} | {cat_str}")
            if not is_original and summary["meaningful"]:
                print(f"               ⚠️ ALTERNATE ENTITY HAS CLAIM HISTORY — "
                      f"this patient is known to the payer under {label}")
            time.sleep(0.6)

        all_results.append(case_result)

    # Rollup
    print("\n" + "=" * 90)
    print("\nROLLUP — wrong-entity candidates:\n")
    any_hits = False
    for cr in all_results:
        case = cr["case"]
        hits = [a for a in cr["alternates"]
                if not a["is_original"] and a["summary"].get("meaningful")]
        orig = next((a for a in cr["alternates"] if a["is_original"]), None)
        orig_meaningful = orig and orig["summary"].get("meaningful")
        if hits:
            any_hits = True
            for a in hits:
                print(f"  • {case['first']} {case['last']} ({case['mco']}, DOS {case['dos']})")
                print(f"      billed under: {NPI_TO_LABEL.get(case['billed_npi'], '?')} → "
                      f"original returned: "
                      f"{'(nothing)' if not orig_meaningful else 'claim history'}")
                print(f"      alternate hit: {a['label']} → "
                      f"{a['summary']['adjudication_count']} records, "
                      f"${a['summary']['paid_total']:.2f} paid historically")
                print()
    if not any_hits:
        print("  (no alternate-entity hits in this test set)")

    agg = OUT_DIR / f"{timestamp}_rollup.json"
    agg.write_text(json.dumps(all_results, indent=2, default=str))
    print(f"\nSaved: {agg}")


if __name__ == "__main__":
    asyncio.run(main())
