"""
Detailed end-of-run report for the live claim workflow.

This extends the existing run summary with the business detail the team needs:
findings, what happened to each claim, dollars outstanding, what was fixed
without human intervention, and what still needs a person.
"""
from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from config.models import Claim, DailyRunSummary, ResolutionAction, ResolutionResult
from reporting.report_paths import write_text_report


def generate_end_of_run_report(
    summary: DailyRunSummary,
    *,
    ar_lookup: dict[tuple[str, str], dict[str, Any]] | None = None,
    clickup_task_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    ar_lookup = ar_lookup or {}
    clickup_task_map = clickup_task_map or {}

    claim_rows = [
        build_claim_run_row(
            result,
            ar_lookup=ar_lookup,
            clickup_task_id=clickup_task_map.get(result.claim.claim_id, ""),
        )
        for result in summary.results
    ]

    totals = {
        "claims_reviewed": len(claim_rows),
        "claims_completed": summary.claims_completed,
        "human_review_flags": summary.human_review_flags,
        "billed_total": round(sum(row["billed_amount"] for row in claim_rows), 2),
        "paid_total": round(sum(row["paid_amount"] for row in claim_rows), 2),
        "outstanding_total": round(sum(row["outstanding_balance"] for row in claim_rows), 2),
        "auto_fixed_count": sum(1 for row in claim_rows if row["auto_fixed"]),
        "auto_resubmitted_count": sum(1 for row in claim_rows if row["auto_resubmitted"]),
        "human_needed_count": sum(1 for row in claim_rows if row["human_needed"]),
        "human_needed_outstanding_total": round(
            sum(row["outstanding_balance"] for row in claim_rows if row["human_needed"]),
            2,
        ),
    }

    report = {
        "metadata": {
            "report_type": "daily_run_report",
            "run_date": summary.run_date.isoformat(),
            "generated_at": datetime.now().isoformat(timespec="seconds"),
        },
        "totals": totals,
        "claims": claim_rows,
    }

    md_result = write_text_report(
        "Daily Run Reports",
        "daily_run_report",
        ".md",
        render_markdown(report),
    )
    json_result = write_text_report(
        "Daily Run Reports",
        "daily_run_report",
        ".json",
        json.dumps(report, indent=2),
    )
    report["output"] = {
        "markdown_path": str(md_result.local_path),
        "json_path": str(json_result.local_path),
        "markdown_dropbox_path": md_result.dropbox_path,
        "json_dropbox_path": json_result.dropbox_path,
        "markdown_uploaded_to_dropbox": md_result.uploaded_to_dropbox,
        "json_uploaded_to_dropbox": json_result.uploaded_to_dropbox,
        "markdown_upload_error": md_result.upload_error,
        "json_upload_error": json_result.upload_error,
    }
    return report


def build_claim_run_row(
    result: ResolutionResult,
    *,
    ar_lookup: dict[tuple[str, str], dict[str, Any]],
    clickup_task_id: str = "",
) -> dict[str, Any]:
    claim = result.claim
    ar = lookup_ar_record(claim, ar_lookup)
    paid_amount = _float_value(ar.get("total_received"), default=claim.paid_amount) if ar else claim.paid_amount
    outstanding = _float_value(ar.get("outstanding")) if ar else max(claim.billed_amount - paid_amount, 0.0)
    finding = human_or_note(result)
    return {
        "claim_id": claim.claim_id,
        "client_name": claim.client_name,
        "unique_id": claim.lauris_id,
        "member_id": claim.client_id,
        "dos": _date_text(claim.dos),
        "mco": claim.mco.value,
        "program": claim.program.value,
        "cpt_code": claim.proc_code,
        "service_code": claim.service_code,
        "billed_amount": round(claim.billed_amount, 2),
        "paid_amount": round(paid_amount, 2),
        "outstanding_balance": round(outstanding, 2),
        "ar_status": ar.get("ar_status", "") if ar else "",
        "denial_codes": [code.value for code in claim.denial_codes],
        "finding": finding,
        "denial_reason": claim.denial_reason_raw,
        "payer_api_gateway": getattr(claim, "payer_api_gateway", ""),
        "payer_api_bucket": getattr(claim, "payer_api_bucket", ""),
        "payer_api_reason": getattr(claim, "payer_api_reason", ""),
        "payer_api_detail_summary": getattr(claim, "payer_api_detail_summary", ""),
        "payer_api_detail_items": list(getattr(claim, "payer_api_detail_items", [])),
        "action_taken": result.action_taken.value,
        "what_was_done": result.note_written or result.human_reason or result.action_taken.value,
        "auto_fixed": is_auto_fixed(result),
        "auto_resubmitted": is_auto_resubmitted(result),
        "human_needed": result.needs_human,
        "human_reason": result.human_reason,
        "clickup_task_id": clickup_task_id,
        "success": result.success,
    }


def lookup_ar_record(claim: Claim, ar_lookup: dict[tuple[str, str], dict[str, Any]]) -> dict[str, Any]:
    key = (claim.client_id, _date_text(claim.dos))
    return ar_lookup.get(key, {})


def build_ar_lookup(ar_claims: list[dict[str, Any]]) -> dict[tuple[str, str], dict[str, Any]]:
    lookup: dict[tuple[str, str], dict[str, Any]] = {}
    for item in ar_claims:
        member_id = str(item.get("member_id", "")).strip()
        dos = str(item.get("doc_date", "")).strip()[:10]
        if member_id and dos and (member_id, dos) not in lookup:
            lookup[(member_id, dos)] = item
    return lookup


def render_markdown(report: dict[str, Any]) -> str:
    totals = report["totals"]
    lines = [
        "# Daily Run Report",
        "",
        f"Run date: {report['metadata']['run_date']}",
        f"Generated: {report['metadata']['generated_at']}",
        "",
        "## Totals",
        "",
        f"- Claims reviewed: {totals['claims_reviewed']}",
        f"- Claims completed: {totals['claims_completed']}",
        f"- Human review flags: {totals['human_review_flags']}",
        f"- Total billed: ${totals['billed_total']:.2f}",
        f"- Total paid: ${totals['paid_total']:.2f}",
        f"- Total outstanding / balance due: ${totals['outstanding_total']:.2f}",
        f"- Auto-fixed claims: {totals['auto_fixed_count']}",
        f"- Auto-resubmitted claims: {totals['auto_resubmitted_count']}",
        f"- Human-needed claims: {totals['human_needed_count']}",
        f"- Outstanding dollars still needing human help: ${totals['human_needed_outstanding_total']:.2f}",
        "",
        "## Auto-Fixed / Resubmitted Without Human Intervention",
        "",
    ]

    auto_rows = [row for row in report["claims"] if row["auto_fixed"] or row["auto_resubmitted"]]
    if auto_rows:
        for row in auto_rows:
            lines.append(
                f"- Claim {row['claim_id']} | DOS {row['dos']} | {row['client_name']} | "
                f"{row['program']} | CPT {row['cpt_code'] or 'blank'} | "
                f"Denials {', '.join(row['denial_codes']) or 'none'} | "
                f"Outstanding ${row['outstanding_balance']:.2f} | "
                f"Payer API: {row['payer_api_detail_summary'] or row['payer_api_reason'] or 'none'} | "
                f"{row['what_was_done']}"
            )
    else:
        lines.append("- None")

    lines.extend([
        "",
        "## Human Intervention Required",
        "",
    ])

    human_rows = [row for row in report["claims"] if row["human_needed"]]
    if human_rows:
        for row in human_rows:
            task = row["clickup_task_id"] or "none"
            lines.append(
                f"- Claim {row['claim_id']} | DOS {row['dos']} | {row['client_name']} | "
                f"{row['program']} | CPT {row['cpt_code'] or 'blank'} | "
                f"Denials {', '.join(row['denial_codes']) or 'none'} | "
                f"Outstanding ${row['outstanding_balance']:.2f} | "
                f"Payer API: {row['payer_api_detail_summary'] or row['payer_api_reason'] or 'none'} | "
                f"Why: {row['human_reason'] or row['finding']} | "
                f"ClickUp task: {task}"
            )
    else:
        lines.append("- None")

    lines.extend([
        "",
        "## Claim Details",
        "",
        "| Claim | Unique ID | DOS | MCO | Program | CPT | Denial Codes | Claim.MD Reason | Payer API Findings | Billed | Paid | Outstanding | Done | Human? | ClickUp |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ])
    for row in report["claims"]:
        lines.append(
            "| "
            f"{row['claim_id']} | "
            f"{row['unique_id'] or 'blank'} | "
            f"{row['dos']} | "
            f"{row['mco']} | "
            f"{row['program']} | "
            f"{row['cpt_code'] or 'blank'} | "
            f"{_md(', '.join(row['denial_codes']) or 'none')} | "
            f"{_md(row['denial_reason'])} | "
            f"{_md(row['payer_api_detail_summary'] or row['payer_api_reason'])} | "
            f"${row['billed_amount']:.2f} | "
            f"${row['paid_amount']:.2f} | "
            f"${row['outstanding_balance']:.2f} | "
            f"{_md(row['what_was_done'])} | "
            f"{'Yes' if row['human_needed'] else 'No'} | "
            f"{row['clickup_task_id'] or 'blank'} |"
        )
    lines.append("")
    return "\n".join(lines)


def is_auto_fixed(result: ResolutionResult) -> bool:
    return (
        result.success
        and not result.needs_human
        and result.action_taken in {
            ResolutionAction.CORRECT_AND_RESUBMIT,
            ResolutionAction.LAURIS_FIX_COMPANY,
            ResolutionAction.REPROCESS_LAURIS,
        }
    )


def is_auto_resubmitted(result: ResolutionResult) -> bool:
    return (
        result.success
        and not result.needs_human
        and result.action_taken in {
            ResolutionAction.CORRECT_AND_RESUBMIT,
            ResolutionAction.RECONSIDERATION,
            ResolutionAction.MCO_PORTAL_AUTH_CHECK,
        }
    )


def human_or_note(result: ResolutionResult) -> str:
    if result.human_reason:
        return result.human_reason
    if result.note_written:
        return result.note_written
    return result.action_taken.value


def _date_text(value: date | datetime | str) -> str:
    if isinstance(value, (date, datetime)):
        return value.isoformat()[:10]
    return str(value or "")[:10]


def _float_value(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _md(value: Any) -> str:
    return str(value or "").replace("|", "\\|").replace("\n", " ")
