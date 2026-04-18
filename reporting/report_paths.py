"""
Shared report output paths.

All generated business reports should land in the Dropbox Claim Resolution
folder and use unique filenames so repeat runs never overwrite earlier reports.
"""
from __future__ import annotations

import os
import re
import json
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import requests

CLAIM_RESOLUTION_REPORT_DIR = Path(
    os.getenv(
        "CLAIM_RESOLUTION_REPORT_DIR",
        (
            "/Users/nicholasmoyer/Library/CloudStorage/"
            "Dropbox-LifeConsultantsInc/Chesapeake LCI/"
            "AR Reports/Claim Resolution"
        ),
    )
)
DROPBOX_REPORT_ROOT = os.getenv(
    "DROPBOX_CLAIM_RESOLUTION_REPORT_ROOT",
    "/Chesapeake LCI/AR Reports/Claim Resolution",
)
LOCAL_FALLBACK_REPORT_DIR = Path(
    os.getenv(
        "LOCAL_FALLBACK_REPORT_DIR",
        str(Path(tempfile.gettempdir()) / "claim_resolution_reports"),
    )
)


@dataclass(frozen=True)
class ReportWriteResult:
    local_path: Path
    dropbox_path: str
    uploaded_to_dropbox: bool = False
    upload_error: str = ""

    @property
    def display_path(self) -> str:
        if self.uploaded_to_dropbox:
            return self.dropbox_path
        return str(self.local_path)


def report_type_dir(report_type: str) -> Path:
    """Return the Dropbox subfolder for a report type."""
    return CLAIM_RESOLUTION_REPORT_DIR / _safe_part(report_type)


def unique_report_stem(
    report_type: str,
    prefix: str,
    *,
    when: datetime | None = None,
) -> Path:
    """
    Return a unique path stem, without extension, under the report type folder.

    The timestamp includes microseconds. The final existence check is still
    kept so an unusually fast repeat call cannot overwrite a report.
    """
    when = when or datetime.now()
    folder = _writable_report_type_dir(report_type)
    base_name = f"{_safe_part(prefix)}_{when.strftime('%Y%m%d_%H%M%S_%f')}"
    stem = folder / base_name
    if not stem.exists() and not any(folder.glob(f"{base_name}.*")):
        return stem

    counter = 1
    while True:
        candidate = folder / f"{base_name}_{counter:03d}"
        if not candidate.exists() and not any(folder.glob(f"{candidate.name}.*")):
            return candidate
        counter += 1


def unique_report_path(
    report_type: str,
    prefix: str,
    suffix: str,
    *,
    when: datetime | None = None,
) -> Path:
    """Return a unique report file path with the requested suffix."""
    suffix = suffix if suffix.startswith(".") else f".{suffix}"
    path = unique_report_stem(report_type, prefix, when=when).with_suffix(suffix)
    if not path.exists():
        return path

    counter = 1
    while True:
        candidate = path.with_name(f"{path.stem}_{counter:03d}{path.suffix}")
        if not candidate.exists():
            return candidate
        counter += 1


def write_text_report(
    report_type: str,
    prefix: str,
    suffix: str,
    content: str,
    *,
    when: datetime | None = None,
    encoding: str = "utf-8",
) -> ReportWriteResult:
    """Write a text report locally and upload to Dropbox when needed."""
    path = unique_report_path(report_type, prefix, suffix, when=when)
    path.write_text(content, encoding=encoding)
    return sync_report_file(path, report_type)


def write_bytes_report(
    report_type: str,
    prefix: str,
    suffix: str,
    content: bytes,
    *,
    when: datetime | None = None,
) -> ReportWriteResult:
    """Write a binary report locally and upload to Dropbox when needed."""
    path = unique_report_path(report_type, prefix, suffix, when=when)
    path.write_bytes(content)
    return sync_report_file(path, report_type)


def sync_report_file(path: Path, report_type: str) -> ReportWriteResult:
    """
    Upload a report file to Dropbox if the local Dropbox folder was unavailable.

    If the report was saved directly into the local Dropbox folder, Dropbox sync
    handles the cloud upload and this returns uploaded_to_dropbox=False.
    """
    path = Path(path)
    dropbox_path = dropbox_report_path(report_type, path.name)
    if _is_inside(path, CLAIM_RESOLUTION_REPORT_DIR):
        return ReportWriteResult(local_path=path, dropbox_path=dropbox_path)

    ok, error = upload_file_to_dropbox(path, dropbox_path)
    return ReportWriteResult(
        local_path=path,
        dropbox_path=dropbox_path,
        uploaded_to_dropbox=ok,
        upload_error=error,
    )


def dropbox_report_path(report_type: str, filename: str) -> str:
    """Return the Dropbox API path for a report file."""
    root = "/" + DROPBOX_REPORT_ROOT.strip("/")
    return f"{root}/{_safe_part(report_type)}/{filename}"


def upload_file_to_dropbox(local_path: Path, dropbox_path: str) -> tuple[bool, str]:
    """
    Upload a file using Dropbox's HTTP API.

    Configure one of:
    - DROPBOX_ACCESS_TOKEN
    - DROPBOX_REFRESH_TOKEN + DROPBOX_APP_KEY + DROPBOX_APP_SECRET
    """
    token = _dropbox_access_token()
    if not token:
        return False, (
            "Dropbox API credentials are not configured. Set DROPBOX_ACCESS_TOKEN "
            "or DROPBOX_REFRESH_TOKEN + DROPBOX_APP_KEY + DROPBOX_APP_SECRET."
        )

    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/octet-stream",
        "Dropbox-API-Arg": json.dumps({
            "path": dropbox_path,
            "mode": "add",
            "autorename": True,
            "mute": False,
            "strict_conflict": False,
        }),
    }
    try:
        response = requests.post(
            "https://content.dropboxapi.com/2/files/upload",
            headers=headers,
            data=Path(local_path).read_bytes(),
            timeout=120,
        )
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)

    if 200 <= response.status_code < 300:
        return True, ""
    return False, f"Dropbox upload failed: HTTP {response.status_code} {response.text[:300]}"


def latest_report_path(report_type: str, pattern: str) -> Path | None:
    """Return the most recently modified matching report, if one exists."""
    folder = report_type_dir(report_type)
    if not folder.exists():
        return None
    matches = [path for path in folder.glob(pattern) if path.is_file()]
    if not matches:
        return None
    return max(matches, key=lambda path: path.stat().st_mtime)


def _writable_report_type_dir(report_type: str) -> Path:
    folder = report_type_dir(report_type)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        test_path = folder / ".write_test"
        test_path.write_text("ok")
        test_path.unlink(missing_ok=True)
        return folder
    except OSError:
        fallback = LOCAL_FALLBACK_REPORT_DIR / _safe_part(report_type)
        fallback.mkdir(parents=True, exist_ok=True)
        return fallback


def _dropbox_access_token() -> str:
    token = os.getenv("DROPBOX_ACCESS_TOKEN", "")
    if token:
        return token

    refresh_token = os.getenv("DROPBOX_REFRESH_TOKEN", "")
    app_key = os.getenv("DROPBOX_APP_KEY", "")
    app_secret = os.getenv("DROPBOX_APP_SECRET", "")
    if not (refresh_token and app_key and app_secret):
        return ""

    try:
        response = requests.post(
            "https://api.dropboxapi.com/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            auth=(app_key, app_secret),
            timeout=60,
        )
        if response.status_code >= 400:
            return ""
        return response.json().get("access_token", "")
    except Exception:  # noqa: BLE001
        return ""


def _is_inside(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
        return True
    except ValueError:
        return False


def _safe_part(value: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(value or "").strip())
    return safe.strip("_") or "report"
