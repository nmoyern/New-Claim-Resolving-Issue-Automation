"""
Single source of truth for LCI billing entities.

Use this module whenever code needs to map company/program, billing NPI,
Tax ID/EIN, Claim.MD region, or Availity provider metadata.
"""
from __future__ import annotations

from dataclasses import dataclass

from config.models import Program


@dataclass(frozen=True)
class BillingEntity:
    key: str
    program: Program
    display_name: str
    claimmd_region: str
    billing_npi: str
    tax_id: str
    availity_submitter_id: str
    availity_provider_name: str
    address_line1: str = ""
    city: str = ""
    state: str = ""
    zip_code: str = ""
    dmas_license_pdf: str = ""


MARYS_HOME = BillingEntity(
    key="MARYS_HOME",
    program=Program.MARYS_HOME,
    display_name="Mary's Home Inc",
    claimmd_region="Mary's Home Inc",
    billing_npi="1437871753",
    tax_id="861567663",
    availity_submitter_id="1636587",
    availity_provider_name="MARYS HOME INC",
    address_line1="4020 PORTSMOUTH BLVD",
    city="CHESAPEAKE",
    state="VA",
    zip_code="23321",
    dmas_license_pdf="data/dmas_licenses/marys_home_dmas_license.pdf",
)

NHCS = BillingEntity(
    key="NHCS",
    program=Program.NHCS,
    display_name="New Heights Community Support",
    claimmd_region="NHCS",
    billing_npi="1700297447",
    tax_id="465232420",
    availity_submitter_id="628128",
    availity_provider_name="NEW HEIGHTS COMMUNITY SUPPORT",
    address_line1="407 E CHURCH ST",
    city="MARTINSVILLE",
    state="VA",
    zip_code="24112",
    dmas_license_pdf="data/dmas_licenses/nhcs_dmas_license.pdf",
)

KJLN = BillingEntity(
    key="KJLN",
    program=Program.KJLN,
    display_name="KJLN Inc",
    claimmd_region="KJLN",
    billing_npi="1306491592",
    tax_id="821966562",
    availity_submitter_id="977164",
    availity_provider_name="KJLN INC",
    address_line1="4020 PORTSMOUTH BLVD",
    city="CHESAPEAKE",
    state="VA",
    zip_code="23321",
    dmas_license_pdf="data/dmas_licenses/kjln_dmas_license.pdf",
)

ENTITIES: tuple[BillingEntity, ...] = (MARYS_HOME, NHCS, KJLN)


def get_all_entities() -> tuple[BillingEntity, ...]:
    return ENTITIES


def get_entity_by_program(program: Program | str) -> BillingEntity | None:
    program_value = program.value if isinstance(program, Program) else str(program)
    for entity in ENTITIES:
        if entity.program.value == program_value or entity.key == program_value:
            return entity
    return None


def get_entity_by_npi(npi: str) -> BillingEntity | None:
    npi = str(npi or "").strip()
    for entity in ENTITIES:
        if entity.billing_npi == npi:
            return entity
    return None


def get_entity_by_claimmd_region(region: str) -> BillingEntity | None:
    needle = _norm(region)
    if not needle:
        return None
    for entity in ENTITIES:
        candidates = {
            entity.key,
            entity.display_name,
            entity.claimmd_region,
            entity.availity_provider_name,
        }
        if needle in {_norm(c) for c in candidates}:
            return entity
    return None


def entity_npi_map() -> dict[str, str]:
    return {entity.key: entity.billing_npi for entity in ENTITIES}


def entity_program_map() -> dict[Program, BillingEntity]:
    return {entity.program: entity for entity in ENTITIES}


def availity_entity_map() -> dict[str, tuple[str, str, str]]:
    """
    Return the legacy Availity script format:
    {entity display name: (submitter.id, billing NPI, providers.lastName)}.
    """
    return {
        entity.display_name: (
            entity.availity_submitter_id,
            entity.billing_npi,
            entity.availity_provider_name,
        )
        for entity in ENTITIES
    }


def _norm(value: str) -> str:
    return "".join(ch for ch in str(value or "").upper() if ch.isalnum())
