"""
Lauris DOB/gender lookup for payer API calls.

The payer auth/company lookup needs DOB for reliable Optum matching. Lauris
already exposes the needed patient demographics through the
Claim_DOB__x0026__Gender_AUTOMATION XML view.
"""
from __future__ import annotations

import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import date, datetime
from functools import lru_cache
from typing import Iterable

from config.models import Claim
from logging_utils.logger import get_logger
from sources.lauris_xml import fetch_xml_text

logger = get_logger("lauris_demographics")

LAURIS_DEMOGRAPHICS_URL = (
    "https://www12.laurisonline.com/reports/formsearchdataviewXML.aspx"
    "?viewid=5DipytbGOyfOZovnw%2fd01Q%3d%3d"
)


@dataclass(frozen=True)
class LaurisDemographics:
    lauris_id: str
    full_name: str
    first: str
    last: str
    dob: str
    gender_code: str
    closed_id: str = ""
    billing_summary_id: str = ""
    pcn: str = ""


@lru_cache(maxsize=1)
def fetch_lauris_demographics() -> dict[str, LaurisDemographics]:
    """
    Return demographics keyed by Lauris Unique_ID (Key).

    Each patient may have many billing rows; the dict keeps one entry per
    patient with the PCN from the most recent row.  The full PCN→patient
    index is built separately by _pcn_demographics_index().
    """
    username = os.environ.get("LAURIS_USERNAME", "")
    password = os.environ.get("LAURIS_PASSWORD", "")
    if not (username and password):
        logger.warning("Lauris credentials missing; demographics unavailable")
        return {}

    response_text = fetch_xml_text(
        LAURIS_DEMOGRAPHICS_URL,
        cache_key="lauris_demographics_v3",
        timeout=300,
    )
    root = ET.fromstring(response_text)
    out: dict[str, LaurisDemographics] = {}
    pcn_index: dict[str, LaurisDemographics] = {}
    for row in root:
        uid = (row.findtext("Key") or "").strip()
        dob = _normalize_dob((row.findtext("DOB") or "").strip())
        if not (uid and dob):
            continue
        full_name = (row.findtext("Name") or "").strip()
        first, last = _split_name(full_name)
        gender_code = _gender_code(row.findtext("Gender") or "")
        closed_id = (row.findtext("Closed_ID") or "").strip()
        bs_id = (row.findtext("Billing_Summary_ID") or "").strip()
        pcn = f"CW{closed_id}-{bs_id}" if closed_id and bs_id else ""

        demo = LaurisDemographics(
            lauris_id=uid,
            full_name=full_name,
            first=first,
            last=last,
            dob=dob,
            gender_code=gender_code,
            closed_id=closed_id,
            billing_summary_id=bs_id,
            pcn=pcn,
        )
        out[uid] = demo
        if pcn:
            pcn_index[pcn.upper()] = demo

    _PCN_INDEX.clear()
    _PCN_INDEX.update(pcn_index)
    logger.info(
        "Lauris demographics fetched",
        patients=len(out),
        pcn_entries=len(pcn_index),
    )
    return out


# Module-level PCN index populated by fetch_lauris_demographics()
_PCN_INDEX: dict[str, LaurisDemographics] = {}


def enrich_claim_with_demographics(
    claim: Claim,
    demographics: dict[str, LaurisDemographics] | None = None,
) -> Claim:
    """Attach DOB/gender attributes to a claim when a Lauris match exists."""
    demo = find_demographics_for_claim(claim, demographics)
    if not demo:
        return claim

    claim.client_dob = demo.dob
    claim.gender_code = demo.gender_code
    claim.patient_full_name = demo.full_name
    claim.patient_first_name = demo.first
    claim.patient_last_name = demo.last
    claim.lauris_id = demo.lauris_id
    if demo.pcn and not getattr(claim, "patient_account_number", ""):
        claim.patient_account_number = demo.pcn
    return claim


def enrich_claims_with_demographics(
    claims: Iterable[Claim],
    demographics: dict[str, LaurisDemographics] | None = None,
) -> list[Claim]:
    demographics = demographics if demographics is not None else fetch_lauris_demographics()
    return [enrich_claim_with_demographics(claim, demographics) for claim in claims]


def find_demographics_for_claim(
    claim: Claim,
    demographics: dict[str, LaurisDemographics] | None = None,
) -> LaurisDemographics | None:
    demographics = demographics if demographics is not None else fetch_lauris_demographics()
    if claim.lauris_id and claim.lauris_id in demographics:
        demo = demographics[claim.lauris_id]
        if _is_usable_patient_name(demo.full_name):
            return demo

    # PCN lookup — match CW{closed_id}-{bs_id} from Claim.MD against Lauris
    pcn_to_check = (
        getattr(claim, "patient_account_number", "") or ""
    ).strip().upper()
    if not pcn_to_check:
        pcn_to_check = (claim.client_name or "").strip().upper()
    if _looks_like_patient_account_number(pcn_to_check):
        pcn_index = _pcn_demographics_index(demographics)
        demo = pcn_index.get(pcn_to_check)
        if demo:
            return demo

    bridge_demo = _billing_bridge_demographics(claim, demographics)
    if bridge_demo:
        return bridge_demo

    claim_first, claim_last = _split_name(claim.client_name)
    if claim_first and claim_last and not _looks_like_patient_account_number(claim.client_name):
        for demo in demographics.values():
            if not _is_usable_patient_name(demo.full_name):
                continue
            if _norm_name(demo.first) == _norm_name(claim_first) and _norm_name(demo.last) == _norm_name(claim_last):
                return demo
    return None


def _split_name(name: str) -> tuple[str, str]:
    raw = str(name or "").strip()
    if not raw:
        return "", ""
    if "," in raw:
        last, first = raw.split(",", 1)
        return first.strip().upper(), last.strip().upper()
    parts = raw.split()
    if len(parts) < 2:
        return "", ""
    return parts[0].upper(), parts[-1].upper()


def _normalize_dob(raw: str) -> str:
    raw = str(raw or "").strip()
    if not raw:
        return ""
    if len(raw) >= 10 and raw[4:5] == "-" and raw[7:8] == "-":
        return raw[:10]
    for fmt in ("%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(raw[:10], fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    return raw[:10]


def _gender_code(raw: str) -> str:
    value = str(raw or "").strip().lower()
    if value.startswith("f"):
        return "F"
    if value.startswith("m"):
        return "M"
    return "U"


def _norm_name(value: str) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalpha())


def _is_usable_patient_name(name: str) -> bool:
    first, last = _split_name(name)
    return bool(first and last)


def _billing_bridge_demographics(
    claim: Claim,
    demographics: dict[str, LaurisDemographics],
) -> LaurisDemographics | None:
    bridge_lookup = _billing_bridge_lookup()
    member_id = str(getattr(claim, "client_id", "") or "").strip()
    dos = getattr(claim, "dos", None)
    dos_str = dos.isoformat()[:10] if hasattr(dos, "isoformat") else str(dos or "")[:10]
    if not (member_id and dos_str):
        return None
    billing_row = bridge_lookup.get((member_id, dos_str))
    if billing_row is None:
        return None

    lauris_id = str(
        billing_row.get("lauris_id", "") or billing_row.get("key", "") or ""
    ).strip()
    if lauris_id and not claim.lauris_id:
        claim.lauris_id = lauris_id
    auth_number = str(billing_row.get("auth_number", "") or "").strip()
    if auth_number and not getattr(claim, "auth_number", ""):
        claim.auth_number = auth_number
    billing_name = str(billing_row.get("name", "") or "").strip()
    if billing_name and not getattr(claim, "patient_full_name", ""):
        claim.patient_full_name = billing_name
        first, last = _split_name(billing_name)
        if first and last:
            claim.patient_first_name = first
            claim.patient_last_name = last
    if not lauris_id:
        return None
    return demographics.get(lauris_id)


@lru_cache(maxsize=1)
def _billing_bridge_lookup() -> dict[tuple[str, str], dict[str, str]]:
    from sources.lauris_xml import fetch_claim_member_bridge

    lookup: dict[tuple[str, str], dict[str, str]] = {}
    try:
        for row in fetch_claim_member_bridge(lookback_days=365):
            member_id = str(row.get("member_id", "") or "").strip()
            doc_date = str(row.get("doc_date", "") or "").strip()[:10]
            if member_id and doc_date:
                lookup[(member_id, doc_date)] = row
    except Exception as exc:  # noqa: BLE001
        logger.warning("Billing bridge lookup unavailable", error=str(exc))
    return lookup


def _pcn_demographics_index(
    demographics: dict[str, LaurisDemographics],
) -> dict[str, LaurisDemographics]:
    """Return the PCN-keyed index built during fetch_lauris_demographics().

    Falls back to building from the demographics dict if the module-level
    index hasn't been populated yet.
    """
    if _PCN_INDEX:
        return _PCN_INDEX
    return {
        demo.pcn.upper(): demo
        for demo in demographics.values()
        if demo.pcn
    }


def _looks_like_patient_account_number(value: str) -> bool:
    raw = str(value or "").strip().upper()
    return raw.startswith("CW") and "-" in raw
