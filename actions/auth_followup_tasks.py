"""
Grouped ClickUp follow-up for missing authorization denials.

No-auth denied claims must not be resubmitted until the authorization is found
or supplied by a human. These helpers group requests by Lauris Unique ID so one
person does not receive a separate ClickUp task per claim line.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from config.models import Claim, DenialCode
from logging_utils.logger import get_logger

logger = get_logger("auth_followup_tasks")


@dataclass
class AuthFollowupGroup:
    unique_id: str
    client_name: str
    claims: list[Claim] = field(default_factory=list)


def needs_authorization_before_resubmission(claim: Claim) -> bool:
    """Return True when a claim needs auth obtained before resubmission."""
    return (
        DenialCode.NO_AUTH in (claim.denial_codes or [])
        and not (claim.auth_number or "").strip()
    )


def group_claims_by_unique_id(claims: list[Claim]) -> list[AuthFollowupGroup]:
    """Group missing-auth claims by Lauris Unique ID, with safe fallback."""
    groups: dict[str, AuthFollowupGroup] = {}
    for claim in claims:
        if not needs_authorization_before_resubmission(claim):
            continue
        unique_id = _unique_id_for_claim(claim)
        if unique_id not in groups:
            groups[unique_id] = AuthFollowupGroup(
                unique_id=unique_id,
                client_name=claim.client_name,
            )
        groups[unique_id].claims.append(claim)
    return list(groups.values())


async def create_missing_auth_clickup_tasks(claims: list[Claim]) -> dict[str, str]:
    """
    Create or update one ClickUp task per Unique ID for missing auth claims.

    The task explicitly asks staff to provide the authorization before any
    resubmission, and lists CPT code/program for every affected claim.
    """
    from actions.clickup_tasks import ClickUpTaskCreator

    task_creator = ClickUpTaskCreator()
    claim_task_map: dict[str, str] = {}
    grouped = group_claims_by_unique_id(claims)
    for group in grouped:
        claim_ids = ", ".join(claim.claim_id for claim in group.claims)
        issue = _issue_text(group)
        history = _history_text(group)
        dos_summary = _dos_summary(group.claims)
        needed = (
            "Obtain or confirm the approved authorization before this claim is "
            "resubmitted. Reply with the auth number, auth date range, approved "
            "company/entity, date(s) of service, program, CPT code/service, "
            "and whether the claim should be billed under Mary's Home, NHCS, "
            "or KJLN."
        )
        task_id = await task_creator.create_or_update_patient_task(
            patient_name=group.client_name,
            patient_key=group.unique_id,
            client_id=group.unique_id,
            claim_id=claim_ids,
            issue=issue,
            history=history,
            role="billing",
            needed=needed,
            task_name=f"Missing Auth Before Resubmission — {group.client_name} — DOS {dos_summary}",
        )
        if task_id:
            for claim in group.claims:
                claim_task_map[claim.claim_id] = task_id
    logger.info(
        "Grouped missing-auth ClickUp tasks created",
        groups=len({task_id for task_id in claim_task_map.values()}),
        claims=sum(len(group.claims) for group in grouped),
    )
    return claim_task_map


def _issue_text(group: AuthFollowupGroup) -> str:
    lines = [
        "Denied claim(s) have no authorization on the claim.",
        "Do not resubmit until the authorization is obtained/confirmed.",
        f"Lauris Unique ID: {group.unique_id}",
        f"Date(s) of service in this request: {_dos_summary(group.claims)}",
        "",
        "Affected claim lines:",
    ]
    for claim in group.claims:
        lines.append(
            "- "
            f"Claim {claim.claim_id} | DOS {claim.dos} | MCO {claim.mco.value} | "
            f"Program {claim.program.value} | CPT {claim.proc_code or 'blank'} | "
            f"Service {claim.service_code or 'blank'} | "
            f"Billed ${claim.billed_amount:.2f}"
        )
    return "\n".join(lines)


def _history_text(group: AuthFollowupGroup) -> str:
    lines = [
        "The automation checked the denied claim and found no auth number on the claim.",
        "It stopped before resubmission because authorization is required first.",
    ]
    for claim in group.claims:
        raw_reason = claim.denial_reason_raw or "No raw denial reason captured."
        lines.append(
            f"- Claim {claim.claim_id} | DOS {claim.dos} | "
            f"CPT {claim.proc_code or 'blank'} | Program {claim.program.value}: "
            f"{raw_reason[:180]}"
        )
    return "\n".join(lines)


def _unique_id_for_claim(claim: Claim) -> str:
    if (claim.lauris_id or "").strip():
        return claim.lauris_id.strip()
    if (claim.client_id or "").strip():
        return f"member:{claim.client_id.strip()}"
    return f"name:{claim.client_name.strip().lower()}"


def _dos_summary(claims: list[Claim]) -> str:
    dates = sorted({_date_text(claim.dos) for claim in claims if claim.dos})
    if not dates:
        return "unknown"
    if len(dates) == 1:
        return dates[0]
    return ", ".join(dates[:3]) + (f" (+{len(dates) - 3} more)" if len(dates) > 3 else "")


def _date_text(value: date) -> str:
    return value.isoformat()
