"""
Company/auth match classifier.

This module answers one business question:

    Does the payer authorization match the company used on the claim?

It classifies only. It does not modify Claim.MD. When exactly one different
entity matches, it returns the fields that should be changed later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from config.entities import (
    BillingEntity,
    get_all_entities,
    get_entity_by_claimmd_region,
    get_entity_by_npi,
    get_entity_by_program,
)
from config.models import Claim
from logging_utils.logger import get_logger

logger = get_logger("company_auth_match")


@dataclass(frozen=True)
class AuthLookupResult:
    found: bool
    entity: BillingEntity
    auth_number: str = ""
    reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CompanyAuthMatchResult:
    claim_id: str
    status: str
    current_entity: BillingEntity | None
    matched_entities: tuple[AuthLookupResult, ...]
    recommended_action: str
    reason: str
    fields_to_change: dict[str, str] = field(default_factory=dict)

    @property
    def should_update_claim(self) -> bool:
        return self.status == "mismatch_single_match"

    @property
    def needs_human(self) -> bool:
        return self.status in {"no_auth_match", "multiple_auth_matches", "lookup_error"}


class AuthLookup(Protocol):
    async def check_authorization(
        self,
        claim: Claim,
        entity: BillingEntity,
    ) -> AuthLookupResult:
        """Return whether the payer auth matches this claim under this entity."""


class NoopAuthLookup:
    """Safe default: never guesses an auth match."""

    async def check_authorization(
        self,
        claim: Claim,
        entity: BillingEntity,
    ) -> AuthLookupResult:
        return AuthLookupResult(
            found=False,
            entity=entity,
            reason="No payer authorization lookup is configured.",
        )


async def classify_company_auth_match(
    claim: Claim,
    lookup: AuthLookup | None = None,
) -> CompanyAuthMatchResult:
    """
    Check the claim's current entity first, then sweep all approved entities.

    Results:
    - current_entity_match: auth matches the claim's current entity
    - mismatch_single_match: exactly one different entity matches
    - no_auth_match: no entity matches
    - multiple_auth_matches: more than one entity matches
    - lookup_error: lookup crashed
    """
    lookup = lookup or NoopAuthLookup()
    current_entity = infer_claim_entity(claim)

    try:
        if current_entity:
            current_match = await lookup.check_authorization(claim, current_entity)
            if current_match.found:
                return CompanyAuthMatchResult(
                    claim_id=claim.claim_id,
                    status="current_entity_match",
                    current_entity=current_entity,
                    matched_entities=(current_match,),
                    recommended_action="continue_normal_denial_workflow",
                    reason=(
                        f"Authorization matches current claim entity "
                        f"{current_entity.display_name}."
                    ),
                )

        matches = []
        for entity in get_all_entities():
            if current_entity and entity.key == current_entity.key:
                continue
            result = await lookup.check_authorization(claim, entity)
            if result.found:
                matches.append(result)

    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "Company/auth match lookup failed",
            claim_id=claim.claim_id,
            error=str(exc),
        )
        return CompanyAuthMatchResult(
            claim_id=claim.claim_id,
            status="lookup_error",
            current_entity=current_entity,
            matched_entities=(),
            recommended_action="human_review",
            reason=f"Authorization lookup failed: {str(exc)[:120]}",
        )

    if len(matches) == 1:
        match = matches[0]
        return CompanyAuthMatchResult(
            claim_id=claim.claim_id,
            status="mismatch_single_match",
            current_entity=current_entity,
            matched_entities=(match,),
            recommended_action=f"update_to_{match.entity.key}_and_resubmit",
            reason=(
                f"Authorization matches {match.entity.display_name}, not "
                f"{current_entity.display_name if current_entity else 'the current/known claim entity'}."
            ),
            fields_to_change=_fields_for_entity(match.entity, match.auth_number),
        )

    if len(matches) > 1:
        return CompanyAuthMatchResult(
            claim_id=claim.claim_id,
            status="multiple_auth_matches",
            current_entity=current_entity,
            matched_entities=tuple(matches),
            recommended_action="human_review",
            reason="More than one company matched the authorization; do not guess.",
        )

    return CompanyAuthMatchResult(
        claim_id=claim.claim_id,
        status="no_auth_match",
        current_entity=current_entity,
        matched_entities=(),
        recommended_action="human_review",
        reason="No approved LCI entity matched the payer authorization.",
    )


async def classify_with_payer_lookup(claim: Claim) -> CompanyAuthMatchResult:
    """
    Use the configured Optum/Availity lookup adapter to classify a claim.

    Kept separate from classify_company_auth_match() so unit tests and dry
    review tools can inject fake lookup behavior without network access.
    """
    from sources.payer_auth_lookup import PayerAuthorizationLookup

    return await classify_company_auth_match(claim, PayerAuthorizationLookup())


def infer_claim_entity(claim: Claim) -> BillingEntity | None:
    """
    Infer the claim's current entity using the most claim-specific data first.
    """
    return (
        get_entity_by_npi(claim.npi)
        or get_entity_by_claimmd_region(claim.billing_region)
        or get_entity_by_program(claim.program)
    )


def _fields_for_entity(entity: BillingEntity, auth_number: str = "") -> dict[str, str]:
    fields = {
        "billing_region": entity.claimmd_region,
        "npi": entity.billing_npi,
        "tax_id": entity.tax_id,
    }
    if auth_number:
        fields["auth_number"] = auth_number
    return fields
