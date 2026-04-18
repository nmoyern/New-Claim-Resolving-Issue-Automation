"""
Classification-only dry run report.

This report is for proving the decision tree before any live claim changes.
It reads Claim.MD rejected/denied claims, enriches what it can, asks the
Optum/Availity payer checks when enabled, runs the company/auth classifier,
and writes both JSON and plain-English Markdown.
"""
from __future__ import annotations

import json
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path
from typing import Any

from actions.company_auth_match import classify_with_payer_lookup
from actions.auth_followup_tasks import needs_authorization_before_resubmission
from config.models import Claim
from decision_tree.router import ClaimRouter
from logging_utils.logger import get_logger
from sources.claimmd_api import ClaimMDAPI
from sources.lauris_demographics import (
    enrich_claims_with_demographics,
    fetch_lauris_demographics,
)
from sources.payer_inquiry import check_payer_claim_status, is_billed_rejected_or_denied
from reporting.report_paths import report_type_dir, sync_report_file, unique_report_stem

logger = get_logger("classification_report")

DEFAULT_OUTPUT_DIR = report_type_dir("Classification Dry Runs")


async def run_classification_report(
    *,
    max_claims: int = 50,
    full_pull: bool = False,
    include_payer_api: bool = True,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    """
    Build and save a dry-run report without changing payer, Lauris, or Claim.MD.

    Important guardrail: Claim.MD responses are fetched with save_cursor=False,
    so running this report does not mark responses as already handled.
    """
    started_at = datetime.now()
    api = ClaimMDAPI()
    router = ClaimRouter()

    raw_claims = await api.get_denied_claims(
        full_pull=full_pull,
        save_cursor=False,
    )
    scoped_claims = [claim for claim in raw_claims if is_billed_rejected_or_denied(claim)]
    limited_claims = scoped_claims[:max_claims] if max_claims > 0 else scoped_claims

    demographics_count = 0
    demographics_error = ""
    try:
        demographics = fetch_lauris_demographics()
        limited_claims = enrich_claims_with_demographics(limited_claims, demographics)
        demographics_count = sum(1 for claim in limited_claims if getattr(claim, "client_dob", ""))
    except Exception as exc:  # noqa: BLE001
        demographics_error = str(exc)
        logger.warning("Dry-run demographics enrichment failed", error=demographics_error)

    claim_reports = []
    for claim in limited_claims:
        claim_reports.append(
            await build_claim_classification(
                claim,
                router=router,
                include_payer_api=include_payer_api,
            )
        )

    report = {
        "metadata": {
            "report_type": "classification_dry_run",
            "started_at": started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now().isoformat(timespec="seconds"),
            "full_pull": full_pull,
            "max_claims": max_claims,
            "include_payer_api": include_payer_api,
            "mutates_claimmd": False,
            "posts_eras": False,
            "advances_claimmd_response_cursor": False,
        },
        "era_posture": {
            "normal_live_position": "ERA download/stage/post runs before claim denial classification.",
            "dry_run_action": "Not executed in this classification-only report.",
            "why": "The report is meant to prove decisions without posting payments or changing records.",
        },
        "counts": {
            "claimmd_rejected_denied_seen": len(raw_claims),
            "billed_rejected_denied_in_scope": len(scoped_claims),
            "included_in_report": len(limited_claims),
            "skipped_by_scope": max(0, len(raw_claims) - len(scoped_claims)),
            "skipped_by_limit": max(0, len(scoped_claims) - len(limited_claims)),
            "demographics_enriched": demographics_count,
            "demographics_missing": max(0, len(limited_claims) - demographics_count),
        },
        "demographics": {
            "attempted": True,
            "error": demographics_error,
        },
        "claims": claim_reports,
    }

    json_path, md_path = write_report_files(report, output_dir=output_dir)
    json_path.write_text(json.dumps(report, indent=2, default=_json_default))
    md_path.write_text(render_markdown(report))
    json_sync = sync_report_file(json_path, "Classification Dry Runs")
    md_sync = sync_report_file(md_path, "Classification Dry Runs")
    report["output"] = {
        "json_path": str(json_sync.local_path),
        "markdown_path": str(md_sync.local_path),
        "json_dropbox_path": json_sync.dropbox_path,
        "markdown_dropbox_path": md_sync.dropbox_path,
        "json_uploaded_to_dropbox": json_sync.uploaded_to_dropbox,
        "markdown_uploaded_to_dropbox": md_sync.uploaded_to_dropbox,
        "json_upload_error": json_sync.upload_error,
        "markdown_upload_error": md_sync.upload_error,
    }
    json_path.write_text(json.dumps(report, indent=2, default=_json_default))
    md_path.write_text(render_markdown(report))
    sync_report_file(json_path, "Classification Dry Runs")
    sync_report_file(md_path, "Classification Dry Runs")
    return report


async def build_claim_classification(
    claim: Claim,
    *,
    router: ClaimRouter | None = None,
    include_payer_api: bool = True,
) -> dict[str, Any]:
    """Classify one claim and preserve each decision step for review."""
    router = router or ClaimRouter()
    previous_context = claim_previous_context(claim)
    missing_auth_gate = needs_authorization_before_resubmission(claim)
    steps: list[dict[str, Any]] = [
        {
            "step": "scope_filter",
            "result": "included",
            "plain_english": "Claim was billed and Claim.MD says it is rejected or denied.",
        },
        {
            "step": "lauris_demographics",
            "result": "found" if getattr(claim, "client_dob", "") else "missing",
            "plain_english": (
                "DOB/gender are available for payer matching."
                if getattr(claim, "client_dob", "")
                else "DOB/gender were not available from Lauris for this report."
            ),
        },
    ]
    if missing_auth_gate:
        steps.append({
            "step": "authorization_required_gate",
            "result": "blocked_before_resubmission",
            "plain_english": (
                "Claim has a no-authorization denial and no auth number on the "
                "claim. The system must obtain/confirm the authorization before "
                "resubmitting."
            ),
            "clickup_group_key": claim.lauris_id or claim.client_id or claim.client_name,
            "cpt_code": claim.proc_code,
            "program": claim.program.value,
        })

    payer_result: dict[str, Any] | None = None
    payer_allows_processing = True
    if include_payer_api:
        try:
            payer = await check_payer_claim_status(claim)
            payer_result = _payer_result_dict(payer)
            payer_allows_processing = payer.should_process
            steps.append({
                "step": "payer_status",
                "result": payer.bucket,
                "plain_english": payer.reason,
                "gateway": payer.gateway,
                "should_process": payer.should_process,
                "payer_detail_summary": payer.detail_summary,
                "payer_detail_items": payer.detail_items,
            })
        except Exception as exc:  # noqa: BLE001
            payer_result = {
                "gateway": "unknown",
                "bucket": "api_error",
                "ok": False,
                "should_process": True,
                "reason": f"Payer status check failed: {str(exc)[:160]}",
            }
            steps.append({
                "step": "payer_status",
                "result": "api_error",
                "plain_english": payer_result["reason"],
                "should_process": True,
            })
    else:
        steps.append({
            "step": "payer_status",
            "result": "skipped",
            "plain_english": "Payer API calls were disabled for this dry run.",
            "should_process": True,
        })

    company_auth_result: dict[str, Any] | None = None
    if include_payer_api and payer_allows_processing:
        try:
            company_auth = await classify_with_payer_lookup(claim)
            company_auth_result = _company_auth_result_dict(company_auth)
            steps.append({
                "step": "company_auth_match",
                "result": company_auth.status,
                "plain_english": company_auth.reason,
                "recommended_action": company_auth.recommended_action,
                "fields_to_change": company_auth.fields_to_change,
            })
        except Exception as exc:  # noqa: BLE001
            company_auth_result = {
                "status": "lookup_error",
                "recommended_action": "human_review",
                "reason": f"Company/auth lookup failed: {str(exc)[:160]}",
                "fields_to_change": {},
            }
            steps.append({
                "step": "company_auth_match",
                "result": "lookup_error",
                "plain_english": company_auth_result["reason"],
                "recommended_action": "human_review",
            })
    elif not payer_allows_processing:
        steps.append({
            "step": "company_auth_match",
            "result": "not_needed",
            "plain_english": "Payer says not to work this claim, so company/auth correction was not checked.",
        })
    else:
        steps.append({
            "step": "company_auth_match",
            "result": "skipped",
            "plain_english": "Payer API calls were disabled, so company/auth matching was not checked.",
        })

    if not payer_allows_processing:
        router_result = {
            "action": "skip_too_new",
            "reason": (payer_result or {}).get("bucket", "payer_said_skip"),
        }
        recommended_action = "Do not work this claim right now."
        human_needed = False
    else:
        action, route_reason = router.route(claim)
        router_result = {"action": action.value, "reason": route_reason}
        steps.append({
            "step": "decision_tree_route",
            "result": action.value,
            "plain_english": f"Decision tree selected {action.value} because of {route_reason}.",
        })
        if company_auth_result and company_auth_result.get("status") == "mismatch_single_match":
            if missing_auth_gate:
                recommended_action = (
                    "Obtain/confirm the authorization first, then update the "
                    "billing company/EIN/NPI/auth fields and resubmit."
                )
                human_needed = True
            else:
                recommended_action = "Update the billing company/EIN/NPI/auth fields, then resubmit."
                human_needed = False
        elif company_auth_result and company_auth_result.get("recommended_action") == "human_review":
            recommended_action = "Send to ClickUp/human review before taking the next step."
            human_needed = True
        elif missing_auth_gate:
            recommended_action = (
                "Request the missing authorization in ClickUp, grouped by Unique ID, "
                "before any resubmission."
            )
            human_needed = True
        elif action.value == "human_review":
            recommended_action = "Send to ClickUp/human review before taking the next step."
            human_needed = True
        else:
            recommended_action = f"Proceed with {action.value}."
            human_needed = False

    return {
        "claim": claim_identity(claim),
        "previous_context": previous_context,
        "steps": steps,
        "payer_status": payer_result,
        "company_auth_match": company_auth_result,
        "router": router_result,
        "recommended_action": recommended_action,
        "human_needed": human_needed,
    }


def claim_identity(claim: Claim) -> dict[str, Any]:
    return {
        "claim_id": claim.claim_id,
        "client_name": claim.client_name,
        "unique_id": claim.lauris_id,
        "member_id": claim.client_id,
        "dos": _date_value(claim.dos),
        "mco": claim.mco.value,
        "program": claim.program.value,
        "billed_amount": claim.billed_amount,
        "paid_amount": claim.paid_amount,
        "claimmd_url": claim.claimmd_url,
        "cpt_code": claim.proc_code,
        "service_code": claim.service_code,
    }


def claim_previous_context(claim: Claim) -> dict[str, Any]:
    return {
        "status": claim.status.value,
        "denial_codes": [code.value for code in claim.denial_codes],
        "denial_reason_raw": claim.denial_reason_raw,
        "date_billed": _date_value(claim.date_billed),
        "date_denied": _date_value(claim.date_denied),
        "last_note": claim.last_note,
        "last_followup": _date_value(claim.last_followup),
        "recon_submitted": _date_value(claim.recon_submitted),
        "appeal_submitted": _date_value(claim.appeal_submitted),
        "auth_number": claim.auth_number,
        "billing_region": claim.billing_region,
        "npi": claim.npi,
        "lauris_id": claim.lauris_id,
        "client_dob": getattr(claim, "client_dob", ""),
        "gender_code": getattr(claim, "gender_code", ""),
        "service_code": claim.service_code,
        "proc_code": claim.proc_code,
        "units": claim.units,
        "rate_per_unit": claim.rate_per_unit,
        "age_days": claim.age_days,
    }


def write_report_files(
    report: dict[str, Any],
    *,
    output_dir: Path = DEFAULT_OUTPUT_DIR,
) -> tuple[Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir == DEFAULT_OUTPUT_DIR:
        base = unique_report_stem("Classification Dry Runs", "classification")
    else:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        base = output_dir / f"classification_{stamp}"
    json_path = base.with_suffix(".json")
    md_path = base.with_suffix(".md")
    json_path.write_text(json.dumps(report, indent=2, default=_json_default))
    md_path.write_text(render_markdown(report))
    sync_report_file(json_path, "Classification Dry Runs")
    sync_report_file(md_path, "Classification Dry Runs")
    return json_path, md_path


def render_markdown(report: dict[str, Any]) -> str:
    metadata = report["metadata"]
    counts = report["counts"]
    lines = [
        "# Claim Classification Dry Run",
        "",
        f"Run started: {metadata['started_at']}",
        "",
        "## What this run did",
        "",
        "- Looked at Claim.MD rejected/denied claims.",
        "- Kept only claims that were actually billed.",
        "- Checked Lauris demographics when available.",
        "- Checked Optum for United/UHC and Availity for the other MCOs when enabled.",
        "- Ran the company/auth match decision tree.",
        "- Did not post ERAs, change Claim.MD, or advance the Claim.MD response cursor.",
        "",
        "## ERA posture",
        "",
        f"- Normal live workflow: {report['era_posture']['normal_live_position']}",
        f"- This dry run: {report['era_posture']['dry_run_action']}",
        f"- Reason: {report['era_posture']['why']}",
        "",
        "## Counts",
        "",
        f"- Claim.MD rejected/denied seen: {counts['claimmd_rejected_denied_seen']}",
        f"- Billed rejected/denied in scope: {counts['billed_rejected_denied_in_scope']}",
        f"- Included in this report: {counts['included_in_report']}",
        f"- Skipped because not in scope: {counts['skipped_by_scope']}",
        f"- Skipped because of report limit: {counts['skipped_by_limit']}",
        f"- Lauris demographics found: {counts['demographics_enriched']}",
        f"- Lauris demographics missing: {counts['demographics_missing']}",
        "",
        "## Claim summary",
        "",
        "| Claim | Client | MCO | Claim.MD status | Payer result | Company/auth | Router | Human? |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for item in report["claims"]:
        claim = item["claim"]
        context = item["previous_context"]
        payer = item.get("payer_status") or {}
        company = item.get("company_auth_match") or {}
        router = item.get("router") or {}
        human = "Yes" if item.get("human_needed") else "No"
        lines.append(
            "| "
            f"{_md(claim['claim_id'])} | "
            f"{_md(claim['client_name'])} | "
            f"{_md(claim['mco'])} | "
            f"{_md(context['status'])} | "
            f"{_md(payer.get('bucket', 'skipped'))} | "
            f"{_md(company.get('status', 'skipped'))} | "
            f"{_md(router.get('action', 'not_routed'))} | "
            f"{human} |"
        )

    for index, item in enumerate(report["claims"], 1):
        claim = item["claim"]
        context = item["previous_context"]
        lines.extend([
            "",
            f"## {index}. {claim['client_name']} - {claim['claim_id']}",
            "",
            f"Recommended action: {item['recommended_action']}",
            "",
            "Previous claim context:",
            "",
            f"- MCO/program: {claim['mco']} / {claim['program']}",
            f"- Unique ID: {claim.get('unique_id') or context['lauris_id'] or 'blank'}",
            f"- CPT code/program: {claim.get('cpt_code') or 'blank'} / {claim['program']}",
            f"- DOS: {claim['dos']}",
            f"- Billed/paid: ${claim['billed_amount']:.2f} / ${claim['paid_amount']:.2f}",
            f"- Denial codes: {', '.join(context['denial_codes']) or 'none captured'}",
            f"- Raw denial reason: {context['denial_reason_raw'] or 'none captured'}",
            f"- Payer API summary: {(item.get('payer_status') or {}).get('detail_summary') or 'none captured'}",
            f"- Current billing region: {context['billing_region'] or 'blank'}",
            f"- Current NPI: {context['npi'] or 'blank'}",
            f"- Auth number: {context['auth_number'] or 'blank'}",
            f"- Last note: {context['last_note'] or 'none captured'}",
            "",
            "Steps taken in this dry run:",
            "",
        ])
        for step in item["steps"]:
            lines.append(
                f"- {step['step']}: {step['result']} - {step['plain_english']}"
            )
        company = item.get("company_auth_match") or {}
        fields = company.get("fields_to_change") or {}
        if fields:
            lines.extend([
                "",
                "Fields the system would change later, after approval/live mode:",
            ])
            for key, value in fields.items():
                lines.append(f"- {key}: {value}")
        payer_status = item.get("payer_status") or {}
        payer_detail_items = payer_status.get("detail_items") or []
        if payer_detail_items:
            lines.extend([
                "",
                "Payer API findings:",
            ])
            for detail in payer_detail_items:
                lines.append(f"- {detail}")

    lines.append("")
    return "\n".join(lines)


def _payer_result_dict(result: Any) -> dict[str, Any]:
    return {
        "gateway": result.gateway,
        "bucket": result.bucket,
        "ok": result.ok,
        "should_process": result.should_process,
        "reason": result.reason,
        "paid_amount": result.paid_amount,
        "detail_summary": result.detail_summary,
        "detail_items": result.detail_items,
    }


def _company_auth_result_dict(result: Any) -> dict[str, Any]:
    return {
        "claim_id": result.claim_id,
        "status": result.status,
        "current_entity": result.current_entity.key if result.current_entity else "",
        "matched_entities": [
            {
                "entity": match.entity.key,
                "auth_number": match.auth_number,
                "reason": match.reason,
            }
            for match in result.matched_entities
        ],
        "recommended_action": result.recommended_action,
        "reason": result.reason,
        "fields_to_change": result.fields_to_change,
        "should_update_claim": result.should_update_claim,
        "needs_human": result.needs_human,
    }


def _date_value(value: Any) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    return str(value or "")


def _json_default(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "value"):
        return value.value
    try:
        return asdict(value)
    except TypeError:
        return str(value)


def _md(value: Any) -> str:
    text = str(value or "")
    return text.replace("|", "\\|").replace("\n", " ")
