"""
actions/fax_tracker.py
-----------------------
Comprehensive fax tracking system for LCI Claims Automation.

Tracks all faxes across 4 sources:
  1. Lauris Fax History (sent SRAs via Lauris fax proxy)
  2. Nextiva nmoyern sent faxes
  3. Nextiva nmoyern2 sent faxes
  4. Nextiva nmoyern received faxes (auth approvals/rejections)

Stores all fax records in SQLite `data/claims_history.db` (fax_log table).
Provides query helpers for the auth verification cascade.
"""
from __future__ import annotations

import asyncio
import email as email_lib
import imaplib
import os
import re
import sqlite3
from datetime import date, datetime, timedelta
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from config.settings import DRY_RUN, get_credentials, NEXTIVA_FAX_URL
from sources.browser_base import BrowserSession
from lauris.billing import LaurisSession
from logging_utils.logger import get_logger

logger = get_logger("fax_tracker")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "claims_history.db"

# ---------------------------------------------------------------------------
# MCO Fax Number Mapping (reverse: fax number -> MCO name)
# ---------------------------------------------------------------------------

MCO_FAX_NUMBERS: Dict[str, str] = {
    os.getenv("FAX_HUMANA", "9316503707"):        "Humana",
    os.getenv("FAX_SENTARA", "7579639620"):       "Sentara Health Plans",
    os.getenv("FAX_SENTARA_CRISIS", "8443483719"): "Sentara Health Plans",
    os.getenv("FAX_ANTHEM", "8444456646"):        "Anthem",
    os.getenv("FAX_MOLINA", "8553398179"):        "Molina",
    os.getenv("FAX_AETNA", "8337571583"):         "Aetna",
}

# Forward mapping: MCO name -> primary fax number
MCO_TO_FAX: Dict[str, str] = {
    "humana":   os.getenv("FAX_HUMANA", "9316503707"),
    "sentara":  os.getenv("FAX_SENTARA", "7579639620"),
    "anthem":   os.getenv("FAX_ANTHEM", "8444456646"),
    "molina":   os.getenv("FAX_MOLINA", "8553398179"),
    "aetna":    os.getenv("FAX_AETNA", "8337571583"),
}


def fax_number_to_mco(fax_number: str) -> str:
    """Map a fax number to an MCO name. Returns 'unknown' if not recognized."""
    cleaned = re.sub(r"[^\d]", "", fax_number)
    # Try exact match first
    if cleaned in MCO_FAX_NUMBERS:
        return MCO_FAX_NUMBERS[cleaned]
    # Try last 10 digits (strip country code)
    if len(cleaned) > 10:
        cleaned = cleaned[-10:]
        if cleaned in MCO_FAX_NUMBERS:
            return MCO_FAX_NUMBERS[cleaned]
    return "unknown"


def mco_name_to_fax(mco_name: str) -> str:
    """Map an MCO name to its primary fax number. Returns empty string if unknown."""
    key = mco_name.lower().strip()
    # Direct match
    if key in MCO_TO_FAX:
        return MCO_TO_FAX[key]
    # Partial match
    for mco_key, fax in MCO_TO_FAX.items():
        if mco_key in key or key in mco_key:
            return fax
    return ""


# ---------------------------------------------------------------------------
# SQLite fax_log table setup
# ---------------------------------------------------------------------------

def _get_db() -> sqlite3.Connection:
    """Get a connection to the claims history database."""
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_fax_log_table():
    """Create the fax_log table if it does not exist."""
    conn = _get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS fax_log (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            fax_id        TEXT UNIQUE NOT NULL,
            source        TEXT NOT NULL,
            direction     TEXT NOT NULL DEFAULT 'sent',
            fax_date      TEXT,
            company       TEXT DEFAULT '',
            mco           TEXT DEFAULT '',
            fax_number    TEXT DEFAULT '',
            client_name   TEXT DEFAULT '',
            auth_dates    TEXT DEFAULT '',
            auth_number   TEXT DEFAULT '',
            auth_status   TEXT DEFAULT 'unknown',
            document_type TEXT DEFAULT 'unknown',
            reviewed_at   TEXT,
            scan_completed TEXT DEFAULT '',
            notes         TEXT DEFAULT ''
        )
    """)
    # Add columns if table already exists without them
    for col, col_type in [
        ("scan_completed", "TEXT DEFAULT ''"),
        ("entity_verified", "INTEGER DEFAULT 0"),
        ("entity_verified_at", "TEXT DEFAULT ''"),
        ("entity_verified_for", "TEXT DEFAULT ''"),
    ]:
        try:
            conn.execute(f"ALTER TABLE fax_log ADD COLUMN {col} {col_type}")
        except sqlite3.OperationalError:
            pass
    # Indices for common queries
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fax_log_client
        ON fax_log (client_name)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fax_log_source
        ON fax_log (source)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fax_log_mco
        ON fax_log (mco)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fax_log_direction
        ON fax_log (direction)
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_fax_log_fax_date
        ON fax_log (fax_date)
    """)
    conn.commit()
    conn.close()
    logger.info("fax_log table initialized", db=str(DB_PATH))


# Initialize table on import
init_fax_log_table()


# ---------------------------------------------------------------------------
# Database helpers
# ---------------------------------------------------------------------------

def _fax_exists(conn: sqlite3.Connection, fax_id: str) -> bool:
    """Check if a fax_id already exists in the log."""
    row = conn.execute(
        "SELECT 1 FROM fax_log WHERE fax_id = ?", (fax_id,)
    ).fetchone()
    return row is not None


def _insert_fax(conn: sqlite3.Connection, entry: dict) -> bool:
    """Insert a fax entry. Returns True if inserted, False if duplicate."""
    if _fax_exists(conn, entry["fax_id"]):
        return False
    conn.execute("""
        INSERT INTO fax_log
            (fax_id, source, direction, fax_date, company, mco,
             fax_number, client_name, auth_dates, auth_number,
             auth_status, document_type, reviewed_at,
             scan_completed, notes)
        VALUES
            (:fax_id, :source, :direction, :fax_date, :company,
             :mco, :fax_number, :client_name, :auth_dates,
             :auth_number, :auth_status, :document_type,
             :reviewed_at, :scan_completed, :notes)
    """, {
        "fax_id":          entry.get("fax_id", ""),
        "source":          entry.get("source", ""),
        "direction":       entry.get("direction", "sent"),
        "fax_date":        entry.get("fax_date", ""),
        "company":         entry.get("company", ""),
        "mco":             entry.get("mco", ""),
        "fax_number":      entry.get("fax_number", ""),
        "client_name":     entry.get("client_name", ""),
        "auth_dates":      entry.get("auth_dates", ""),
        "auth_number":     entry.get("auth_number", ""),
        "auth_status":     entry.get("auth_status", "unknown"),
        "document_type":   entry.get("document_type", "unknown"),
        "reviewed_at":     entry.get("reviewed_at",
                                     datetime.now().isoformat()),
        "scan_completed":  entry.get("scan_completed", ""),
        "notes":           entry.get("notes", ""),
    })
    return True


def cross_reference_with_lauris(conn: sqlite3.Connection, fax_id: str, fax_number: str, fax_date: str) -> dict:
    """Check if a matching Lauris fax_log entry exists and return its client data.

    Matches by fax_number (same MCO fax) and date (same day).
    This allows Nextiva sent fax entries to inherit client names from
    the corresponding Lauris Fax Status Report entries, which have
    text-based PDFs with reliable client name extraction.

    Args:
        conn: SQLite connection.
        fax_id: The Nextiva fax_id (for logging).
        fax_number: The destination fax number (digits only).
        fax_date: The fax date string from Nextiva grid.

    Returns:
        Dict with client_name, company, service_type, auth_dates, auth_number
        from the matching Lauris entry, or empty dict if no match.
    """
    if not fax_number or not fax_date:
        return {}

    # Normalize fax_date to just the date portion for LIKE matching.
    # Nextiva dates may be "03/24/2026 10:30 AM" or "2026-03-24".
    date_prefix = ""
    for fmt in ("%m/%d/%Y", "%Y-%m-%d", "%m/%d/%Y %I:%M %p", "%m/%d/%Y %I:%M:%S %p"):
        try:
            dt = datetime.strptime(fax_date.strip().split()[0] if " " in fax_date else fax_date.strip(), fmt.split()[0])
            # Use both formats for LIKE matching
            date_prefix = dt.strftime("%m/%d/%Y")
            break
        except (ValueError, IndexError):
            continue

    if not date_prefix:
        # Fallback: use the raw date string as prefix
        date_prefix = fax_date.strip().split()[0] if " " in fax_date else fax_date.strip()

    # Clean fax number — strip country code
    clean_fax = fax_number
    if len(clean_fax) == 11 and clean_fax.startswith("1"):
        clean_fax = clean_fax[1:]

    row = conn.execute(
        """SELECT client_name, company, document_type, auth_dates, auth_number, notes
           FROM fax_log
           WHERE source = 'lauris_sent'
             AND fax_number = ?
             AND fax_date LIKE ?
           ORDER BY id DESC
           LIMIT 1""",
        (clean_fax, f"{date_prefix}%"),
    ).fetchone()

    if row:
        result = {}
        row_dict = dict(row)
        if row_dict.get("client_name"):
            result["client_name"] = row_dict["client_name"]
        if row_dict.get("company"):
            result["company"] = row_dict["company"]
        if row_dict.get("auth_dates"):
            result["auth_dates"] = row_dict["auth_dates"]
        if row_dict.get("auth_number"):
            result["auth_number"] = row_dict["auth_number"]
        # Extract service_type from notes if present
        notes = row_dict.get("notes", "")
        svc_match = re.search(r"Service:\s*(\S+)", notes)
        if svc_match:
            result["service_type"] = svc_match.group(1)
        if row_dict.get("document_type") and row_dict["document_type"] != "unknown":
            result["document_type"] = row_dict["document_type"]
        if result:
            logger.debug(
                "Lauris cross-reference match found",
                fax_id=fax_id,
                lauris_client=result.get("client_name", ""),
            )
        return result

    return {}


def get_last_reviewed_date(source: str) -> Optional[datetime]:
    """
    Returns the most recent fax_date for a given source.
    Used to only scrape new faxes since last review.
    """
    conn = _get_db()
    row = conn.execute(
        "SELECT MAX(fax_date) as max_date FROM fax_log WHERE source = ?",
        (source,),
    ).fetchone()
    conn.close()
    if row and row["max_date"]:
        try:
            return datetime.fromisoformat(row["max_date"])
        except (ValueError, TypeError):
            # Try other date formats
            for fmt in ("%m/%d/%Y", "%Y-%m-%d %H:%M:%S", "%m/%d/%Y %I:%M %p"):
                try:
                    return datetime.strptime(row["max_date"], fmt)
                except ValueError:
                    continue
    return None


# ---------------------------------------------------------------------------
# Fuzzy name matching helper
# ---------------------------------------------------------------------------

def _fuzzy_match(name1: str, name2: str, threshold: float = 0.6) -> bool:
    """Check if two names match with fuzzy logic."""
    if not name1 or not name2:
        return False
    n1 = name1.lower().strip()
    n2 = name2.lower().strip()
    # Exact match
    if n1 == n2:
        return True
    # One name contains the other
    if n1 in n2 or n2 in n1:
        return True
    # Check last name match (most reliable)
    parts1 = n1.split()
    parts2 = n2.split()
    if parts1 and parts2:
        # Last name match
        if parts1[-1] == parts2[-1]:
            return True
        # First+last match
        if len(parts1) >= 2 and len(parts2) >= 2:
            if parts1[0] == parts2[0] and parts1[-1] == parts2[-1]:
                return True
    # SequenceMatcher ratio
    return SequenceMatcher(None, n1, n2).ratio() >= threshold


# ---------------------------------------------------------------------------
# PDF download directory
# ---------------------------------------------------------------------------

FAX_PDF_DIR = Path("/tmp/fax_downloads")

# Max new PDFs to download per scrape run (avoid hammering the server)
MAX_PDF_DOWNLOADS_PER_RUN = 20


# ---------------------------------------------------------------------------
# 1. PDF extraction helper
# ---------------------------------------------------------------------------

OCR_TOOL = "/tmp/fax_downloads/ocr_tool"
_rapid_engine = None


def _get_rapid_engine():
    global _rapid_engine
    if _rapid_engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _rapid_engine = RapidOCR()
    return _rapid_engine


def _ocr_pdf(pdf_path: str, start_page: int = 0, max_pages: int = 1) -> str:
    """
    OCR an image-based PDF. Tries RapidOCR first (~2-3s/page),
    falls back to Apple Vision (~1.5s/page).
    """
    try:
        import fitz  # PyMuPDF

        doc = fitz.open(pdf_path)
        end_page = min(len(doc), start_page + max_pages)
        full_text = ""

        for i in range(start_page, end_page):
            pg = doc[i]
            pix = pg.get_pixmap(dpi=150)
            img_path = f"/tmp/fax_downloads/_ocr_page_{i}.png"
            pix.save(img_path)

            page_text = ""
            # Try RapidOCR first
            try:
                engine = _get_rapid_engine()
                result, _ = engine(img_path)
                if result:
                    page_text = "\n".join(line[1] for line in result)
            except Exception:
                pass

            # Fallback to Apple Vision if RapidOCR failed or empty
            if not page_text.strip():
                import subprocess
                try:
                    r = subprocess.run(
                        [OCR_TOOL, img_path],
                        capture_output=True, text=True, timeout=15,
                    )
                    page_text = r.stdout
                except Exception:
                    pass

            full_text += page_text + "\n"
            logger.debug("OCR page", page=i + 1, chars=len(page_text))

        doc.close()
        return full_text

    except ImportError:
        logger.warning("PyMuPDF not available")
        return ""
    except Exception as e:
        logger.warning("OCR failed", path=pdf_path, error=str(e))
        return ""


def _extract_info_from_fax_pdf(pdf_path: str) -> dict:
    """
    Extract client name, diagnosis, auth number, auth dates, and service type
    from a downloaded Lauris fax PDF.

    Page 1 is typically the cover sheet (redundant with grid data).
    Page 2 is the SERVICE AUTHORIZATION FORM with service type, client name.
    Page 3+ is clinical narrative with diagnosis codes.
    """
    result = {
        "client_name": "",
        "diagnosis": "",
        "auth_number": "",
        "auth_dates": "",
        "service_type": "",
        "entity": "",
    }

    try:
        from pypdf import PdfReader

        reader = PdfReader(pdf_path)
        full_text = ""
        for pg in reader.pages:
            extracted = pg.extract_text() or ""
            full_text += extracted + "\n"

        # If pypdf got no text, the PDF is image-based — use OCR
        if not full_text.strip():
            # Smart page selection:
            # - Nextiva sent PDFs: SRA on page 1 (no cover) → OCR page 1
            # - Lauris PDFs: cover on page 1, SRA on page 2 → OCR page 2
            # - Received faxes: cover + auth letter → OCR pages 1-3
            full_text = _ocr_pdf(pdf_path, start_page=0, max_pages=1)
            if not full_text.strip():
                return result

            cover_indicators = ["facsimile", "recipient company", "NOTICE:"]
            fax_cover = ["From:", "To:", "Page:", "EDT", "EST"]
            is_lauris_cover = any(
                ind.lower() in full_text.lower() for ind in cover_indicators
            )
            is_fax_cover = any(
                ind in full_text for ind in fax_cover
            ) and len(full_text) < 500

            if (is_lauris_cover or is_fax_cover) and len(reader.pages) >= 2:
                # Cover page detected — OCR pages 2-3 for content
                pages_to_ocr = min(3, len(reader.pages) - 1)
                full_text += "\n" + _ocr_pdf(
                    pdf_path, start_page=1, max_pages=pages_to_ocr
                )

        # --- Service type ---
        service_patterns = [
            (r"MENTAL\s+HEALTH\s+SKILL[- ]BUILDING\s*\(?MHSS\)?", "MHSS"),
            (r"MHSS\s+H0046", "MHSS"),
            (r"\bMHSS\b", "MHSS"),
            (r"CRISIS\s+(?:STABILIZATION|INTERVENTION)", "Crisis"),
            (r"INTENSIVE\s+IN[- ]HOME", "IIH"),
            (r"THERAPEUTIC\s+DAY\s+TREATMENT", "TDT"),
            (r"PSYCHOSOCIAL\s+REHAB", "PSR"),
            (r"COMMUNITY\s+STABILIZATION", "Community Stabilization"),
            (r"H0046", "MHSS"),
            (r"H2011", "Crisis"),
            (r"H0032", "IIH"),
        ]
        for pattern, svc_type in service_patterns:
            if re.search(pattern, full_text, re.IGNORECASE):
                result["service_type"] = svc_type
                break

        # --- ICD-10 diagnosis codes (F-codes) ---
        # Try full format first: "F33.1 Major Depressive Disorder..."
        diag_matches = re.findall(
            r"(F[0-9]{2}(?:\.[0-9]{1,2})?)\s+([A-Z][a-zA-Z\s,()-]+)",
            full_text,
        )
        if diag_matches:
            code, desc = diag_matches[0]
            result["diagnosis"] = f"{code} {desc.strip()}"[:200]
        else:
            # OCR fallback: F-code on its own line or after "Diagnosis"
            diag_match = re.search(
                r"(?:Primary\s+)?(?:ICD[- ]?10\s+)?[Dd]iagnos[a-z]*\s*[:\s]*\n?\s*(F[0-9]{2}(?:\.[0-9]{1,2})?)",
                full_text,
            )
            if diag_match:
                result["diagnosis"] = diag_match.group(1).strip()
            else:
                # Just find any standalone F-code
                f_match = re.search(r"\b(F[0-9]{2}\.[0-9]{1,2})\b", full_text)
                if f_match:
                    result["diagnosis"] = f_match.group(1)

        # --- Auth number patterns ---
        auth_patterns = [
            # MCO response letter: "Authorization/ReferenceNumber:260326820"
            r"(?:Authorization|Reference)\s*/?\s*(?:Reference)?\s*Number\s*[:\s]*(\d{6,20})",
            # "Auth #: 12345678" or "Authorization Number: ABC12345"
            r"(?:Auth|Authorization)\s*(?:#|No\.?|Number)\s*[:\s]+([A-Z0-9]{6,20})",
            # "Approved Auth: 12345678"
            r"(?:Approved|Assigned)\s*(?:Auth)?\s*(?:#|Number)?\s*[:\s]+([A-Z0-9]{6,20})",
        ]
        for pattern in auth_patterns:
            m = re.search(pattern, full_text, re.IGNORECASE)
            if m:
                candidate = m.group(1).strip()
                # Skip false positives
                skip_words = {"orized", "ization", "request", "form", "initial"}
                if candidate.lower() not in skip_words and len(candidate) >= 6:
                    result["auth_number"] = candidate
                    break

        # --- Auth date ranges ---
        date_range_patterns = [
            # Lauris SRA format: "06/20/2026To03/20/2026From" (To date then From date)
            r"(\d{1,2}/\d{1,2}/\d{4})\s*To\s*(\d{1,2}/\d{1,2}/\d{4})\s*From",
            # Standard: "From MM/DD/YYYY To MM/DD/YYYY"
            r"From\s*[:\s]*(\d{1,2}/\d{1,2}/\d{4})\s*(?:To|through|thru|-)\s*(\d{1,2}/\d{1,2}/\d{4})",
            # "Period: MM/DD/YYYY - MM/DD/YYYY"
            r"(?:Period|Dates?|Service\s+Date)\s*[:\s]*(\d{1,2}/\d{1,2}/\d{4})\s*(?:-|to|through|thru)\s*(\d{1,2}/\d{1,2}/\d{4})",
            # Generic: MM/DD/YYYY - MM/DD/YYYY
            r"(\d{1,2}/\d{1,2}/\d{4})\s*(?:-|to|through|thru)\s*(\d{1,2}/\d{1,2}/\d{4})",
        ]
        for pattern in date_range_patterns:
            m = re.search(pattern, full_text, re.IGNORECASE)
            if m:
                date1, date2 = m.group(1), m.group(2)
                # For the Lauris "To...From" format, swap: group1=To date, group2=From date
                if "To" in pattern and "From" in pattern and pattern.index("To") < pattern.index("From"):
                    result["auth_dates"] = f"{date2} - {date1}"  # From - To
                else:
                    result["auth_dates"] = f"{date1} - {date2}"
                break

        # --- Client/member name ---
        # Skip words that are NOT names
        _skip_names = {
            "homeless", "n", "na", "plan", "member", "unknown",
            "information", "provider", "organization", "life",
            "consultants", "sentara", "humana", "anthem", "epic",
            "prod", "service", "mental", "health", "the", "this",
            "services", "coordinator", "manager", "supervisor",
            "director", "nurse", "doctor", "data", "dob", "date",
            "address", "phone", "fax", "email", "inc", "llc",
        }

        def _valid_name(name: str) -> bool:
            # Clean: take only first line, strip trailing noise
            name = name.split("\n")[0].strip()
            name = re.sub(r"\s*(DOB|EMR|MRN|SSN|Date|Data|Fax|Phone).*$", "", name, flags=re.IGNORECASE).strip()
            parts = name.split()
            if len(parts) < 2 or len(parts) > 4:
                return False
            if any(p.lower() in _skip_names for p in parts):
                return False
            # Each part should look like a name (start with uppercase, mostly alpha)
            for p in parts:
                clean = p.replace("-", "").replace("'", "").replace(".", "")
                if not clean[0].isupper() or not clean.isalpha():
                    return False
            return True

        def _clean_name(name: str) -> str:
            """Clean extracted name — take first line, strip trailing noise."""
            name = name.split("\n")[0].strip()
            name = re.sub(r"\s*(DOB|EMR|MRN|SSN|Date|Data|Fax|Phone|\(|\#).*$", "", name, flags=re.IGNORECASE).strip()
            return name

        # Try ALL name patterns, collect candidates, pick best
        name_candidates = []

        # "Client Name: Jimmy Williams" or "Client: Jimmy Williams"
        for m in re.finditer(
            r"(?:Client|Patient)\s*(?:Name)?\s*[:\s]+\s*([A-Z][a-zA-Z'-]+(?:[\s]+[A-Z][a-zA-Z'-]+)+)",
            full_text,
        ):
            candidate = _clean_name(m.group(1))
            if _valid_name(candidate):
                name_candidates.append(candidate)

        # "Patient Name: Last, First M (EMR #...)" — Last, First format
        for m in re.finditer(
            r"(?:Patient|Client)\s+Name\s*:\s*([A-Z][a-zA-Z'-]+),\s*([A-Z][a-zA-Z'-]+)",
            full_text,
        ):
            last, first = m.group(1).strip(), m.group(2).strip()
            candidate = f"{first} {last}"
            if _valid_name(candidate):
                name_candidates.append(candidate)

        # "Patient:\nName: First Last" (two lines)
        for m in re.finditer(
            r"(?:Patient|Client)\s*:\s*\n\s*Name\s*:\s*([A-Z][a-zA-Z'-]+(?:\s+[A-Z][a-zA-Z'-]+)+)",
            full_text,
        ):
            candidate = _clean_name(m.group(1))
            if _valid_name(candidate):
                name_candidates.append(candidate)

        # "Patient's Name:\nFirst Last" or "Patient'sName:\nFirst Last"
        for m in re.finditer(
            r"Patient'?s?\s*Name\s*:\s*\n?\s*([A-Z][a-zA-Z'-]+(?:[\s,]+[A-Z][a-zA-Z'-]+)+)",
            full_text,
        ):
            candidate = _clean_name(m.group(1))
            # Handle "Last, First" format
            if "," in candidate:
                parts = candidate.split(",", 1)
                candidate = f"{parts[1].strip()} {parts[0].strip()}"
            if _valid_name(candidate):
                name_candidates.append(candidate)

        # "for C. Davis" or "for First Last" in discharge summaries
        for m in re.finditer(
            r"\bfor\s+([A-Z]\.?\s+[A-Z][a-zA-Z'-]+)\b",
            full_text,
        ):
            candidate = _clean_name(m.group(1))
            if _valid_name(candidate):
                name_candidates.append(candidate)

        # "Name: JIMMY WILLIAMS" (all caps)
        for m in re.finditer(
            r"(?:Client|Patient)\s*(?:Name)?\s*[:\s]+\s*([A-Z]{2,}(?:\s+[A-Z]{2,})+)",
            full_text,
        ):
            candidate = m.group(1).strip().title()
            if _valid_name(candidate):
                name_candidates.append(candidate)

        # "Member's Full Name: First Last"
        m = re.search(
            r"Member'?\s*s?\s+Full\s+Name\s*[:\s]+([A-Z][a-zA-Z'-]+\s+[A-Z][a-zA-Z'-]+)",
            full_text,
        )
        if m and _valid_name(m.group(1)):
            name_candidates.append(m.group(1).strip())

        # "Dear ELEANOR TODD-PREVOST:" or "Dear SHANNON DAVIS,"
        m = re.search(
            r"Dear\s*([A-Z][A-Z'-]+(?:[\s-]+[A-Z][A-Z'-]+)+)\s*[,:;]",
            full_text,
        )
        if m:
            candidate = m.group(1).strip().title()
            if _valid_name(candidate) and candidate.lower() not in ("provider", "sir", "madam"):
                name_candidates.append(candidate)

        # "Discharge summary from ... for C. Davis" or "for First Last"
        m = re.search(
            r"(?:summary|report|notice)\s+(?:from\s+\S+\s+)+for\s+([A-Z]\.?\s*[A-Z][a-zA-Z'-]+)",
            full_text, re.IGNORECASE,
        )
        if m:
            name_candidates.append(m.group(1).strip())

        # "First Last is a XX-year-old"
        m = re.search(
            r"([A-Z][a-zA-Z'-]+\s+[A-Z][a-zA-Z'-]+)\s+is\s+a\s+\d{1,3}[- ]year[- ]old",
            full_text,
        )
        if m and _valid_name(m.group(1)):
            name_candidates.append(m.group(1).strip())

        # Pick first valid candidate
        if name_candidates:
            result["client_name"] = name_candidates[0]

        # Fallback: OCR line-separated "Member First/Last Name:" format
        if not result["client_name"]:
            first_match = re.search(
                r"Member\s+First\s+Name\s*[:\s]*\n\s*([A-Z][a-zA-Z'-]+)",
                full_text,
            )
            last_match = re.search(
                r"Member\s+Last\s+Name\s*[:\s]*\n\s*([A-Z][a-zA-Z'-]+)",
                full_text,
            )
            if first_match and last_match:
                candidate = f"{first_match.group(1).strip()} {last_match.group(1).strip()}"
                if _valid_name(candidate):
                    result["client_name"] = candidate

        # Fallback: Lauris text "HarrisMember Last Name:" format
        if not result["client_name"]:
            lm = re.search(r"([A-Z][a-zA-Z'-]+)\s*Member\s*Last\s*Name", full_text)
            fm = re.search(r"([A-Z][a-zA-Z'-]+)\s*Member\s*First\s*Name", full_text)
            if fm and lm:
                candidate = f"{fm.group(1).strip()} {lm.group(1).strip()}"
                if _valid_name(candidate):
                    result["client_name"] = candidate

        # --- Auth status (approved/denied) from MCO response letters ---
        text_lower = full_text.lower()
        if "denied" in text_lower or "denial" in text_lower:
            result["auth_response"] = "denied"
        elif "approved" in text_lower or "approval" in text_lower:
            result["auth_response"] = "approved"

        # --- Entity (KJLN, NHCS, Mary's Home) ---
        entity_patterns = [
            (r"KJLN\s*Inc", "KJLN Inc"),
            (r"\bKJLN\b", "KJLN Inc"),
            (r"New\s+Heights\s+Community\s+Support", "New Heights Community Support"),
            (r"Mary'?s\s+Home\s+Inc", "Mary's Home Inc."),
            (r"Mary'?s\s+Home", "Mary's Home Inc."),
            (r"\bNHCS\b", "New Heights Community Support"),
        ]
        for pattern, entity_name in entity_patterns:
            if re.search(pattern, full_text, re.IGNORECASE):
                result["entity"] = entity_name
                break

    except Exception as e:
        logger.warning("PDF extraction failed", path=pdf_path, error=str(e))

    return result


# ---------------------------------------------------------------------------
# 2. Lauris Fax Status Scraper (faxstatus.aspx only)
# ---------------------------------------------------------------------------

async def scrape_lauris_fax_history(
    lauris_session: LaurisSession,
    start_date: date,
    end_date: Optional[date] = None,
) -> List[dict]:
    """
    Scrape fax records from Lauris faxstatus.aspx and download PDFs
    via getData.aspx to extract client/auth details.

    URL: {base}/Apps/FaxingBox12/faxstatus.aspx
    Grid: ASPxGridView1_DXMainTable (DevExpress)
    Columns: ID, Recipient_Company, Recipient_Fax, Recipient_Contact,
             Sender_Company, Sender_Address, Sender_Phone, Sender_Contact,
             Comments, Date Added, Status, Send Error, Last Status,
             Submitting User, Failed

    Each row has a magnifying glass link (getData.aspx?faxid={ID}) that
    triggers a PDF download containing the cover sheet, SRA form, and
    clinical narrative.

    Returns list of newly inserted fax entries.
    """
    if end_date is None:
        end_date = date.today()

    new_entries: List[dict] = []
    page = lauris_session.page

    # Ensure PDF download directory exists
    FAX_PDF_DIR.mkdir(parents=True, exist_ok=True)

    try:
        base = lauris_session.login_url.rsplit("/", 1)[0]
        fax_status_url = f"{base}/Apps/FaxingBox12/faxstatus.aspx"
        get_data_base = f"{base}/Apps/FaxingBox12/getData.aspx"

        # -----------------------------------------------------------------
        # Step 1: Navigate to faxstatus.aspx and set filters
        # -----------------------------------------------------------------
        await page.goto(
            fax_status_url,
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await asyncio.sleep(2)

        # Set dropdown to "Sent" (value="1")
        sent_dropdown = await page.query_selector("#ddlSent")
        if sent_dropdown:
            await sent_dropdown.select_option(value="1")

        # Set start date filter
        start_input = await page.query_selector("#txtStartUN")
        if start_input:
            await start_input.fill(start_date.strftime("%m/%d/%Y"))

        # Click Refresh
        refresh_btn = await page.query_selector("#btnRefresh")
        if refresh_btn:
            await refresh_btn.click()
            await asyncio.sleep(3)

        # -----------------------------------------------------------------
        # Step 2: Paginate through all grid pages, collect rows
        # -----------------------------------------------------------------
        all_grid_rows: List[dict] = []
        page_num = 0

        while True:
            page_num += 1
            # Data rows have class 'dxgvDataRow' with 18 cells
            rows = await page.query_selector_all(
                "tr.dxgvDataRow"
            )

            page_row_count = 0
            for row in rows:
                cells = await row.query_selector_all("td")
                if len(cells) < 16:
                    continue

                # DevExpress grid: cell[0]=checkbox, cell[1]=magnify link,
                # cell[2]=ID, cell[3]=Recipient_Company, cell[4]=Recipient_Fax,
                # cell[5]=Recipient_Contact, cell[6]=Sender_Company,
                # cell[7]=Sender_Address, cell[8]=Sender_Phone,
                # cell[9]=Sender_Contact, cell[10]=Comments,
                # cell[11]=Date Added, cell[12]=Status, cell[13]=Send Error,
                # cell[14]=Last Status, cell[15]=Submitting User,
                # cell[16]=Sent, cell[17]=Failed
                try:
                    row_id = (await cells[2].inner_text()).strip()
                except Exception:
                    continue

                # Skip non-numeric IDs
                if not row_id or not row_id.isdigit():
                    continue

                # Get magnifying glass link for PDF download
                link_el = await row.query_selector("a[href*='getData']")
                pdf_url = ""
                if link_el:
                    pdf_url = await link_el.get_attribute("href") or ""

                try:
                    recipient_company = (await cells[3].inner_text()).strip()
                    recipient_fax = (await cells[4].inner_text()).strip()
                    sender_company = (await cells[6].inner_text()).strip()
                    sender_contact = (await cells[9].inner_text()).strip()
                    date_added = (await cells[11].inner_text()).strip()
                    s_status = (await cells[12].inner_text()).strip()
                    submitting_user = (await cells[15].inner_text()).strip()
                    failed = (await cells[17].inner_text()).strip() if len(cells) > 17 else ""
                except Exception:
                    continue

                all_grid_rows.append({
                    "row_id": row_id,
                    "recipient_company": recipient_company,
                    "recipient_fax": recipient_fax,
                    "sender_company": sender_company,
                    "sender_contact": sender_contact,
                    "date_added": date_added,
                    "status": s_status,
                    "submitting_user": submitting_user,
                    "failed": failed,
                    "pdf_url": pdf_url,
                })
                page_row_count += 1

            logger.debug(
                "Grid page scraped",
                page=page_num,
                rows_on_page=page_row_count,
            )

            # Try to go to the next page via DevExpress pagination
            next_btn = await page.query_selector(
                "a[onclick*=\"'PBN'\"], "
                "a[onclick*='PBN'], "
                "a[class*='dxp-button'][class*='dxp-bi-next'], "
                "img[alt='Next'], "
                "td.dxpButton img[alt*='Next']"
            )
            if next_btn:
                is_disabled = await next_btn.get_attribute("class") or ""
                onclick = await next_btn.get_attribute("onclick") or ""
                parent = await next_btn.evaluate_handle(
                    "el => el.closest('td') || el.parentElement"
                )
                parent_class = ""
                if parent:
                    parent_class = await parent.evaluate(
                        "el => el.className || ''"
                    ) or ""
                if ("disabled" in is_disabled.lower()
                        or "disabled" in parent_class.lower()
                        or not onclick):
                    break
                await next_btn.click()
                await asyncio.sleep(2)
            else:
                # Also try evaluating a JS postback for next page
                has_next = await page.evaluate("""
                    () => {
                        const links = document.querySelectorAll('a[onclick]');
                        for (const link of links) {
                            const oc = link.getAttribute('onclick') || '';
                            if (oc.includes('PBN')) return true;
                        }
                        return false;
                    }
                """)
                if has_next:
                    await page.evaluate(
                        "__doPostBack('ASPxGridView1','PBN')"
                    )
                    await asyncio.sleep(2)
                else:
                    break

        logger.info(
            "Lauris fax status rows scraped",
            total_rows=len(all_grid_rows),
            pages=page_num,
        )

        # -----------------------------------------------------------------
        # Step 3: Process rows — skip existing, download PDFs, insert
        # -----------------------------------------------------------------
        conn = _get_db()
        pdfs_downloaded = 0

        for grid_row in all_grid_rows:
            row_id = grid_row["row_id"]
            fax_id = f"lauris_status_{row_id}"

            # Skip if already in log
            if _fax_exists(conn, fax_id):
                continue

            # Determine MCO from recipient company and fax number
            recipient_company = grid_row["recipient_company"]
            fax_number = re.sub(r"[^\d]", "", grid_row["recipient_fax"])
            mco = ""

            # Try fax number first
            if fax_number:
                mco_from_fax = fax_number_to_mco(fax_number)
                if mco_from_fax != "unknown":
                    mco = mco_from_fax

            # Fall back to company name
            if not mco and recipient_company:
                rc_lower = recipient_company.lower()
                for mco_name, mco_label in [
                    ("sentara", "Sentara Health Plans"),
                    ("humana", "Humana"),
                    ("anthem", "Anthem"),
                    ("molina", "Molina"),
                    ("aetna", "Aetna"),
                    ("united", "United"),
                    ("uhc", "United"),
                ]:
                    if mco_name in rc_lower:
                        mco = mco_label
                        break

            # PDF extraction data (defaults)
            pdf_info = {
                "client_name": "",
                "diagnosis": "",
                "auth_number": "",
                "auth_dates": "",
                "service_type": "",
            }

            # Download PDF if under the per-run limit
            pdf_path = ""
            if pdfs_downloaded < MAX_PDF_DOWNLOADS_PER_RUN:
                try:
                    download_url = grid_row.get("pdf_url") or f"{get_data_base}?faxid={row_id}"
                    pdf_path = str(FAX_PDF_DIR / f"fax_{row_id}.pdf")

                    # Use Playwright's download handling
                    async with page.expect_download(timeout=30000) as dl_info:
                        await page.evaluate(
                            f"window.open('{download_url}', '_blank')"
                        )
                    download = await dl_info.value
                    await download.save_as(pdf_path)
                    pdfs_downloaded += 1

                    logger.debug(
                        "Fax PDF downloaded",
                        fax_id=row_id,
                        path=pdf_path,
                    )

                    # Extract info from the PDF
                    pdf_info = _extract_info_from_fax_pdf(pdf_path)

                except Exception as e:
                    logger.warning(
                        "PDF download/extraction failed, logging grid data only",
                        fax_id=row_id,
                        error=str(e),
                    )
                    pdf_path = ""

            # Determine auth_status from grid status
            status_lower = grid_row["status"].lower()
            if "sent" in status_lower or "delivered" in status_lower:
                auth_status = "submitted"
            elif "fail" in status_lower or grid_row["failed"].lower() in ("true", "yes", "1"):
                auth_status = "send_failed"
            else:
                auth_status = "unknown"

            # Determine document type from service type
            doc_type = "sra" if pdf_info["service_type"] else "unknown"

            # Use PDF-extracted client name if available
            client_name = pdf_info["client_name"]

            entry = {
                "fax_id": fax_id,
                "source": "lauris_sent",
                "direction": "sent",
                "fax_date": grid_row["date_added"],
                "company": pdf_info.get("entity") or grid_row["sender_company"],
                "mco": mco,
                "fax_number": fax_number,
                "client_name": client_name,
                "auth_dates": pdf_info["auth_dates"],
                "auth_number": pdf_info["auth_number"],
                "auth_status": auth_status,
                "document_type": doc_type,
                "reviewed_at": datetime.now().isoformat(),
                "notes": (
                    f"ID: {row_id} | Staff: {grid_row['sender_contact']} | "
                    f"User: {grid_row['submitting_user']} | "
                    f"Status: {grid_row['status']}"
                    + (f" | Service: {pdf_info['service_type']}" if pdf_info["service_type"] else "")
                    + (f" | Dx: {pdf_info['diagnosis']}" if pdf_info["diagnosis"] else "")
                    + (f" | PDF: {pdf_path}" if pdf_path else "")
                ),
            }

            if _insert_fax(conn, entry):
                new_entries.append(entry)

        conn.commit()
        conn.close()

        logger.info(
            "Lauris fax history scraped (faxstatus.aspx + PDF extraction)",
            new_count=len(new_entries),
            total_grid_rows=len(all_grid_rows),
            pdfs_downloaded=pdfs_downloaded,
            start=str(start_date),
            end=str(end_date),
        )

    except Exception as e:
        logger.error("Lauris fax history scrape failed", error=str(e))

    return new_entries


# ---------------------------------------------------------------------------
# 2. Nextiva Fax Session (for scraping sent/received via Search page)
# ---------------------------------------------------------------------------

# CSID-to-MCO mapping for incoming faxes (CSID field contains MCO name)
CSID_TO_MCO: Dict[str, str] = {
    "sentarahealthcare": "Sentara Health Plans",
    "sentara":           "Sentara Health Plans",
    "humana":            "Humana",
    "anthem":            "Anthem",
    "molina":            "Molina",
    "aetna":             "Aetna",
    "united":            "United",
    "uhc":               "United",
    "optima":            "Optima",
    "cigna":             "Cigna",
    "magellan":          "Magellan",
}


def _csid_to_mco(csid: str) -> str:
    """Map a CSID string (from incoming fax grid) to an MCO name.

    The CSID field on incoming faxes contains the sender's name, e.g.
    "SentaraHealthcare", "Humana". We normalize and do a fuzzy lookup.
    Returns 'unknown' if not recognized.
    """
    if not csid:
        return "unknown"
    key = re.sub(r"[^a-z]", "", csid.lower())
    # Exact key match
    if key in CSID_TO_MCO:
        return CSID_TO_MCO[key]
    # Substring match
    for csid_key, mco_name in CSID_TO_MCO.items():
        if csid_key in key or key in csid_key:
            return mco_name
    return "unknown"


class NextivaScraperSession(BrowserSession):
    """
    Nextiva fax portal session for scraping sent and received fax logs
    via the Search page (search.aspx inside an iframe).

    Supports both nmoyern and nmoyern2 accounts.
    """
    SESSION_NAME = "nextiva_scraper"

    def __init__(self, account: str = "nmoyern", headless: bool = True):
        self.account = account
        self.SESSION_NAME = f"nextiva_{account}"
        super().__init__(headless=headless)

        if account == "nmoyern2":
            self._username = os.getenv("NEXTIVA_FAX2_USERNAME", "nmoyern2")
            self._password = os.getenv("NEXTIVA_FAX2_PASSWORD", "nextiva123")
        else:
            creds = get_credentials().nextiva
            self._username = creds.username if creds else os.getenv("NEXTIVA_FAX_USERNAME", "nmoyern")
            self._password = creds.password if creds else os.getenv("NEXTIVA_FAX_PASSWORD", "nextiva123")

    @property
    def login_url(self) -> str:
        return NEXTIVA_FAX_URL

    async def _is_logged_in(self) -> bool:
        try:
            url = self.page.url.lower()
            if "xauth" in url or "about:blank" in url:
                # Could still be on the login page — check for the submit button
                login_form = await self.page.query_selector("#xcAppLogonSubmit")
                if login_form:
                    return False
            # Post-login URL contains dashboard.aspx#portal
            if "dashboard.aspx" in url:
                return True
            # Check for post-login nav elements
            for selector in [
                "iframe[name='xcAppNavStack_frame_search']",
                "iframe[id*='xcAppNavStack']",
                ".xsAppNavBar", "#xcAppNavBar",
            ]:
                el = await self.page.query_selector(selector)
                if el:
                    return True
            # Check for SEARCH text in nav (visible after login)
            search_link = await self.page.query_selector("text=SEARCH")
            if search_link:
                return True
            return False
        except Exception:
            return False

    async def _perform_login(self) -> bool:
        await self.page.goto(
            self.login_url, wait_until="load", timeout=30000
        )
        await asyncio.sleep(2)

        if await self._is_logged_in():
            return True

        await self.page.fill("#xcAppLogonUserName", self._username)
        await self.page.fill("#xcAppLogonUserPassword", self._password)

        # Terms checkbox MUST be checked before submit
        terms_cb = await self.page.query_selector("#xcLogonAccordTermsAccept")
        if terms_cb and not await terms_cb.is_checked():
            await terms_cb.check()

        remember_cb = await self.page.query_selector("#xcAppLogonAutoRemember")
        if remember_cb and not await remember_cb.is_checked():
            await remember_cb.check()

        await self.page.click("#xcAppLogonSubmit")

        # Wait for post-login URL (dashboard.aspx#portal)
        try:
            await self.page.wait_for_url(
                "**/dashboard.aspx*", timeout=15000
            )
        except Exception:
            await asyncio.sleep(5)

        return await self._is_logged_in()

    async def navigate_to_search(self):
        """Click SEARCH in the main nav and wait for the search iframe."""
        # Click the SEARCH text link in the main frame nav
        search_link = await self.page.query_selector("text=SEARCH")
        if search_link:
            await search_link.click()
            await asyncio.sleep(3)
        else:
            logger.warning("SEARCH link not found in main nav")

    async def get_search_frame(self):
        """Return the search iframe (xcAppNavStack_frame_search).

        After clicking SEARCH, the search page loads inside an iframe
        named 'xcAppNavStack_frame_search' with URL search.aspx.
        """
        # Wait a moment for iframe to appear
        for _ in range(10):
            for frame in self.page.frames:
                fname = frame.name or ""
                furl = frame.url or ""
                if "frame_search" in fname.lower() or "search.aspx" in furl.lower():
                    return frame
            await asyncio.sleep(1)

        logger.warning("Search iframe not found after waiting")
        return None

    async def set_view_filter(self, frame, filter_value: str):
        """Set the view filter dropdown in the search iframe via JS.

        Args:
            frame: The search iframe.
            filter_value: One of 'all', 'outboundSuccess', 'outboundFail',
                          'inboundSuccess', 'inboundFail'.
        """
        await frame.evaluate(f"""
            () => {{
                const selects = document.querySelectorAll('select');
                if (selects.length > 0) {{
                    selects[0].value = '{filter_value}';
                    selects[0].dispatchEvent(new Event('change', {{bubbles: true}}));
                }}
            }}
        """)
        # Wait for the grid to reload after filter change
        await asyncio.sleep(3)

    async def extract_grid_rows(self, frame) -> List[List[str]]:
        """Extract all data rows from the igGrid in the search iframe.

        Data rows are <tr> elements inside #xcSearchFax_container whose
        first <td> cell contains a numeric message ID (5+ digits).

        Returns list of rows, each row is a list of cell text values.
        """
        rows_data: List[List[str]] = []

        # Wait for the grid container to exist
        try:
            await frame.wait_for_selector(
                "#xcSearchFax_container", timeout=10000
            )
        except Exception:
            logger.warning("Grid container #xcSearchFax_container not found")
            return rows_data

        # Extract rows via JS for speed (avoids many round-trips)
        raw_rows = await frame.evaluate("""
            () => {
                const container = document.querySelector('#xcSearchFax_container');
                if (!container) return [];
                const trs = container.querySelectorAll('tr');
                const result = [];
                for (const tr of trs) {
                    const tds = tr.querySelectorAll('td');
                    if (tds.length < 10) continue;
                    const firstCell = tds[0].innerText.trim();
                    if (!/^\d{5,}$/.test(firstCell)) continue;
                    const cells = [];
                    for (const td of tds) {
                        cells.push(td.innerText.trim());
                    }
                    result.push(cells);
                }
                return result;
            }
        """)

        if raw_rows:
            rows_data.extend(raw_rows)

        return rows_data

    async def get_total_rows(self, frame) -> int:
        """Parse total row count from the pager label.

        The pager shows text like '1 - 100 of 500 rows' inside
        #xcSearchFax_pager_label.
        """
        try:
            label_text = await frame.evaluate("""
                () => {
                    const label = document.querySelector('#xcSearchFax_pager_label');
                    if (!label) return '';
                    const spans = label.querySelectorAll('span');
                    for (const s of spans) {
                        const t = s.innerText.trim();
                        if (/\\d+\\s*-\\s*\\d+\\s+of\\s+\\d+/.test(t)) return t;
                    }
                    return label.innerText.trim();
                }
            """)
            m = re.search(r"of\s+(\d+)", label_text)
            if m:
                return int(m.group(1))
        except Exception:
            pass
        return 0

    async def click_next_page(self, frame) -> bool:
        """Click the next page link in the igGrid pager.

        Returns True if a next page was clicked, False if on last page.
        """
        has_next = await frame.evaluate("""
            () => {
                const nextDiv = document.querySelector('div.ui-iggrid-nextpage');
                if (nextDiv) {
                    const span = nextDiv.querySelector('span.ui-iggrid-nextpagelabel');
                    if (span) {
                        span.click();
                        return true;
                    }
                    nextDiv.click();
                    return true;
                }
                return false;
            }
        """)
        if has_next:
            await asyncio.sleep(3)
        return bool(has_next)

    async def scrape_all_pages(self, frame, max_pages: int = 50) -> List[List[str]]:
        """Extract grid rows from all pages, paginating as needed.

        Args:
            frame: The search iframe.
            max_pages: Maximum number of pages to scrape (safety limit).

        Returns combined list of all row data across all pages.
        """
        all_rows: List[List[str]] = []
        page_num = 0

        while page_num < max_pages:
            page_num += 1
            page_rows = await self.extract_grid_rows(frame)

            if not page_rows:
                logger.debug("No data rows on page", page=page_num)
                break

            all_rows.extend(page_rows)
            logger.debug(
                "Search grid page scraped",
                page=page_num,
                rows_on_page=len(page_rows),
                total_so_far=len(all_rows),
            )

            # Check if there are more pages
            has_next = await self.click_next_page(frame)
            if not has_next:
                break

        return all_rows


# ---------------------------------------------------------------------------
# 3. Nextiva Sent Fax Scraper (via Search page)
# ---------------------------------------------------------------------------

async def _download_nextiva_pdf(
    page,
    frame,
    message_id: str,
    account: str,
    direction: str = "sent",
) -> str:
    """
    Download a PDF for a specific fax row in the Nextiva search grid.

    Clicks the download icon on the row matching message_id, handles the
    download modal (sets format to PDF), and saves the file.

    Args:
        page: The main Playwright page (download events fire here).
        frame: The search iframe containing the grid.
        message_id: The message ID to find in the grid.
        account: Nextiva account name (for filename).
        direction: "sent" or "recv" (for filename).

    Returns:
        Path to the saved PDF, or empty string on failure.
    """
    FAX_PDF_DIR.mkdir(parents=True, exist_ok=True)
    pdf_path = str(FAX_PDF_DIR / f"nextiva_{account}_{direction}_{message_id}.pdf")

    try:
        # Step 1: Click the download icon on the row matching this message_id.
        # The download icon is a span with class xq-download in the Actions
        # cell (cell index 12) of the row whose first cell is the message_id.
        clicked = await frame.evaluate(f"""
            () => {{
                const container = document.querySelector('#xcSearchFax_container');
                if (!container) return false;
                const trs = container.querySelectorAll('tr');
                for (const tr of trs) {{
                    const tds = tr.querySelectorAll('td');
                    if (tds.length < 13) continue;
                    if (tds[0].innerText.trim() === '{message_id}') {{
                        const dlIcon = tds[12].querySelector(
                            'span.xq-download, span[data-xaction="download"]'
                        );
                        if (dlIcon) {{
                            dlIcon.click();
                            return true;
                        }}
                        return false;
                    }}
                }}
                return false;
            }}
        """)

        if not clicked:
            logger.debug("Download icon not found for row", message_id=message_id)
            return ""

        # Step 2: Wait for the download modal to appear
        await asyncio.sleep(2)

        # Step 3: Set format to PDF via JS in the frame
        await frame.evaluate("""
            () => {
                const fmt = document.querySelector('select[name="Format"]');
                if (fmt) {
                    fmt.value = 'pdf';
                    fmt.dispatchEvent(new Event('change', {bubbles: true}));
                }
            }
        """)
        await asyncio.sleep(0.5)

        # Step 4: Click the download button while expecting the download on
        # the main page (download events fire on page, not the iframe).
        async with page.expect_download(timeout=10000) as dl_info:
            # Try clicking the submit button in the modal
            await frame.evaluate("""
                () => {
                    const btn = document.querySelector(
                        'button.xoActionExe.xsSubmit, button.xsSubmit'
                    );
                    if (btn) {
                        btn.click();
                        return true;
                    }
                    // Fallback: any button with text "download"
                    const buttons = document.querySelectorAll('button');
                    for (const b of buttons) {
                        if (b.innerText.trim().toLowerCase() === 'download') {
                            b.click();
                            return true;
                        }
                    }
                    return false;
                }
            """)

        download = await dl_info.value
        await download.save_as(pdf_path)

        logger.debug(
            "Nextiva fax PDF downloaded",
            message_id=message_id,
            path=pdf_path,
        )

        # Step 5: Close the modal (press Escape or click outside)
        try:
            await frame.evaluate("""
                () => {
                    // Try clicking a cancel/close button first
                    const closeBtn = document.querySelector(
                        'button.xsCancel, .xoDialogClose, .xi-close'
                    );
                    if (closeBtn) { closeBtn.click(); return; }
                    // Press Escape as fallback
                    document.dispatchEvent(
                        new KeyboardEvent('keydown', {key: 'Escape', keyCode: 27})
                    );
                }
            """)
        except Exception:
            pass
        await asyncio.sleep(1)

        return pdf_path

    except Exception as e:
        logger.warning(
            "Nextiva PDF download failed",
            message_id=message_id,
            error=str(e),
        )
        # Try to dismiss any open modal so the next download can proceed
        try:
            await frame.evaluate("""
                () => {
                    const closeBtn = document.querySelector(
                        'button.xsCancel, .xoDialogClose, .xi-close'
                    );
                    if (closeBtn) closeBtn.click();
                    document.dispatchEvent(
                        new KeyboardEvent('keydown', {key: 'Escape', keyCode: 27})
                    );
                }
            """)
        except Exception:
            pass
        await asyncio.sleep(1)
        return ""


async def scrape_nextiva_sent_faxes(account: str = "nmoyern", full_scrape: bool = False) -> List[dict]:
    """
    Scrape sent faxes from a Nextiva account's Search page.
    Downloads PDFs for new fax entries and extracts client/auth info via OCR.

    Steps:
      1. Login to Nextiva with the account credentials
      2. Click SEARCH in main frame nav
      3. Wait for xcAppNavStack_frame_search iframe
      4. Set view filter to 'outboundSuccess' via JS on first select
      5. Wait for grid to load
      6. Extract all data rows (message ID in first cell, 14 cells per row)
      7. For each row: message_id, fax_number (cell[2]), fax_date (cell[5]),
         pages (cell[7]), status (cell[8])
      8. Strip country code from fax number, map to MCO
      9. Build fax_id = f"nextiva_{account}_sent_{message_id}"
      10. Skip if already in fax_log
      11. For new rows, download PDF and extract info
      12. Paginate through all pages

    Outgoing row cells (14):
      [0] Message ID, [1] User, [2] Recipient Fax Number (with country code),
      [3] CSID, [4] Caller ID, [5] Fax Date, [6] Tracking ID,
      [7] Pages, [8] Status, [9] Direction, [10-13] Other flags

    Args:
        account: "nmoyern" or "nmoyern2"

    Returns list of newly inserted fax entries.
    """
    source = f"nextiva_{account}_sent"
    new_entries: List[dict] = []

    # Ensure PDF download directory exists
    FAX_PDF_DIR.mkdir(parents=True, exist_ok=True)

    try:
        async with NextivaScraperSession(account=account, headless=True) as session:
            page = session.page

            # Navigate to Search page
            await session.navigate_to_search()
            frame = await session.get_search_frame()
            if not frame:
                logger.error("Could not access search iframe", account=account)
                return new_entries

            # Set view filter to outbound success
            await session.set_view_filter(frame, "outboundSuccess")

            # -----------------------------------------------------------------
            # Early-stop pagination: scrape page by page, stop when all rows
            # on a page already exist in DB (= we've caught up to known data)
            # -----------------------------------------------------------------
            conn = _get_db()
            new_rows: List[Tuple[str, dict]] = []
            page_num = 0

            while page_num < 500:  # paginate through all pages
                page_num += 1
                page_rows = await session.extract_grid_rows(frame)
                if not page_rows:
                    break

                new_on_page = 0
                for row in page_rows:
                    if len(row) < 10:
                        continue
                    msg_id = row[0].strip()
                    if not msg_id or not msg_id.isdigit():
                        continue
                    fax_id = f"nextiva_{account}_sent_{msg_id}"
                    if not _fax_exists(conn, fax_id):
                        new_on_page += 1

                logger.debug(
                    "Nextiva sent page check",
                    page=page_num, total=len(page_rows), new=new_on_page,
                )

                if new_on_page == 0 and not full_scrape:
                    # All rows on this page are known — stop
                    logger.info(
                        "Nextiva sent early stop — all rows on page already known",
                        account=account, stopped_at_page=page_num,
                    )
                    break

                # Process new rows from this page
                for row in page_rows:
                    if len(row) < 10:
                        continue
                    message_id = row[0].strip()
                    if not message_id or not message_id.isdigit():
                        continue
                    fax_id = f"nextiva_{account}_sent_{message_id}"
                    if _fax_exists(conn, fax_id):
                        continue

                    recipient_fax_raw = row[2].strip()
                    fax_date_str = row[5].strip()
                    pages = row[7].strip()
                    status = row[8].strip().lower()

                    fax_number = re.sub(r"[^\d]", "", recipient_fax_raw)
                    if len(fax_number) == 11 and fax_number.startswith("1"):
                        fax_number = fax_number[1:]
                    mco = fax_number_to_mco(fax_number)

                    new_rows.append((fax_id, {
                        "message_id": message_id,
                        "fax_number": fax_number,
                        "fax_date": fax_date_str,
                        "pages": pages,
                        "status": status,
                        "mco": mco,
                    }))

                # Go to next page
                has_next = await session.click_next_page(frame)
                if not has_next:
                    break

            logger.info(
                "Nextiva sent new rows to process",
                account=account, new_rows=len(new_rows),
            )

            # Phase 2 starts below — skip the old row parsing code
            # (jump past the duplicate block)
            if not new_rows:
                conn.close()
                return new_entries

            # Phase 2a: Insert ALL grid rows into DB immediately with
            # empty client_name. This ensures we never lose grid data even
            # if PDF downloads fail later.
            needs_pdf: List[Tuple[str, dict]] = []
            pdfs_downloaded = 0

            for fax_id, row_data in new_rows:
                message_id = row_data["message_id"]
                fax_number = row_data["fax_number"]
                fax_date_str = row_data["fax_date"]
                mco = row_data["mco"]

                # Phase 2b: Cross-reference with Lauris before downloading PDF.
                # Sent faxes are the same SRA docs sent through Lauris, so
                # Lauris fax_log entries already have client names extracted
                # from text-based PDFs.
                lauris_data = cross_reference_with_lauris(
                    conn, fax_id, fax_number, fax_date_str
                )

                client_name = lauris_data.get("client_name", "")
                company = lauris_data.get("company", "") or (mco if mco != "unknown" else "")
                auth_dates = lauris_data.get("auth_dates", "")
                auth_number = lauris_data.get("auth_number", "")
                service_type = lauris_data.get("service_type", "")
                doc_type = lauris_data.get("document_type", "unknown")
                if service_type and doc_type == "unknown":
                    doc_type = "sra"

                cross_ref_note = " | via Lauris cross-ref" if lauris_data else ""

                entry = {
                    "fax_id": fax_id,
                    "source": source,
                    "direction": "sent",
                    "fax_date": fax_date_str,
                    "company": company,
                    "mco": mco,
                    "fax_number": fax_number,
                    "client_name": client_name,
                    "auth_dates": auth_dates,
                    "auth_number": auth_number,
                    "auth_status": (
                        "submitted" if row_data["status"] == "success"
                        else "send_failed"
                    ),
                    "document_type": doc_type,
                    "reviewed_at": datetime.now().isoformat(),
                    "notes": (
                        f"Fax Status: {row_data['status']}"
                        + (f" | Service: {service_type}" if service_type else "")
                        + cross_ref_note
                    ),
                }

                if _insert_fax(conn, entry):
                    new_entries.append(entry)

                # Track rows that had NO Lauris match — these rare edge
                # cases are the only sent faxes that need a PDF download.
                if not lauris_data:
                    needs_pdf.append((fax_id, row_data))

            conn.commit()

            logger.info(
                "Nextiva sent Phase 2a complete — grid data inserted",
                account=account,
                inserted=len(new_entries),
                needs_pdf=len(needs_pdf),
                cross_referenced=len(new_rows) - len(needs_pdf),
            )

            # Phase 2c: Download PDFs ONLY for entries with no Lauris match.
            # This should be a small number (edge cases where Lauris has no
            # record of the fax).
            for fax_id, row_data in needs_pdf:
                if pdfs_downloaded >= MAX_PDF_DOWNLOADS_PER_RUN:
                    break

                message_id = row_data["message_id"]

                pdf_path = await _download_nextiva_pdf(
                    page, frame, message_id, account, direction="sent"
                )
                if pdf_path:
                    pdfs_downloaded += 1
                    pdf_info = _extract_info_from_fax_pdf(pdf_path)
                    logger.debug(
                        "PDF info extracted (no Lauris match)",
                        message_id=message_id,
                        client=pdf_info.get("client_name", ""),
                        auth=pdf_info.get("auth_number", ""),
                    )

                    # Update the already-inserted DB record with PDF data
                    update_fields = {}
                    if pdf_info.get("client_name"):
                        update_fields["client_name"] = pdf_info["client_name"]
                    if pdf_info.get("entity"):
                        update_fields["company"] = pdf_info["entity"]
                    if pdf_info.get("auth_dates"):
                        update_fields["auth_dates"] = pdf_info["auth_dates"]
                    if pdf_info.get("auth_number"):
                        update_fields["auth_number"] = pdf_info["auth_number"]
                    if pdf_info.get("service_type"):
                        update_fields["document_type"] = "sra"

                    if update_fields:
                        set_clause = ", ".join(f"{k} = ?" for k in update_fields)
                        vals = list(update_fields.values()) + [fax_id]
                        conn.execute(
                            f"UPDATE fax_log SET {set_clause} WHERE fax_id = ?",
                            vals,
                        )

                    # Update notes with PDF path
                    conn.execute(
                        "UPDATE fax_log SET notes = notes || ? WHERE fax_id = ?",
                        (
                            (f" | Service: {pdf_info['service_type']}" if pdf_info.get("service_type") else "")
                            + (f" | Dx: {pdf_info['diagnosis']}" if pdf_info.get("diagnosis") else "")
                            + (f" | PDF: {pdf_path}" if pdf_path else ""),
                            fax_id,
                        ),
                    )

                    # Brief pause between downloads
                    await asyncio.sleep(1.5)

            conn.commit()
            conn.close()

    except Exception as e:
        logger.error("Nextiva sent fax scrape failed", account=account, error=str(e))

    logger.info(
        "Nextiva sent fax scrape complete",
        account=account,
        new_count=len(new_entries),
        pdfs_downloaded=pdfs_downloaded if 'pdfs_downloaded' in dir() else 0,
    )
    return new_entries


# ---------------------------------------------------------------------------
# 4. Nextiva Received Fax Scraper (via Search page — CRITICAL)
# ---------------------------------------------------------------------------

async def scrape_nextiva_received_faxes(full_scrape: bool = False) -> List[dict]:
    """
    Scrape received faxes from the nmoyern account's Search page.
    Downloads PDFs for new fax entries and extracts client/auth info via OCR.

    This is especially important for received faxes because:
    - The grid only shows CSID (MCO name) and from number
    - The PDF contains the actual auth approval/rejection with client name,
      auth number, and auth dates
    - OCR extracts this from the image-based fax PDFs

    Steps:
      1. Login to nmoyern account
      2. Click SEARCH, wait for search iframe
      3. Set view filter to 'inboundSuccess' via JS
      4. Wait for grid to load
      5. Extract rows: message_id (cell[0]), our_fax (cell[2]),
         csid_name/MCO (cell[3]), from_phone (cell[4]), fax_date (cell[5]),
         pages (cell[7]), status (cell[8])
      6. MCO comes from cell[3] CSID -- normalize via _csid_to_mco()
      7. Build fax_id = f"nextiva_nmoyern_recv_{message_id}"
      8. For new rows, download PDF and extract info
      9. Paginate through all pages

    Incoming row cells (14):
      [0] Message ID, [1] User, [2] Our Fax Number,
      [3] CSID (MCO name!), [4] From Phone Number,
      [5] Fax Date, [6] Tracking, [7] Pages, [8] Status,
      [9] Direction, [10-13] Other flags

    Returns list of newly inserted fax entries.
    """
    source = "nextiva_nmoyern_received"
    new_entries: List[dict] = []

    # Ensure PDF download directory exists
    FAX_PDF_DIR.mkdir(parents=True, exist_ok=True)

    try:
        async with NextivaScraperSession(account="nmoyern", headless=True) as session:
            page = session.page

            # Navigate to Search page
            await session.navigate_to_search()
            frame = await session.get_search_frame()
            if not frame:
                logger.error("Could not access search iframe for received faxes")
                return new_entries

            # Set view filter to inbound success
            await session.set_view_filter(frame, "inboundSuccess")

            # -----------------------------------------------------------------
            # Early-stop pagination: stop when all rows on a page are known
            # -----------------------------------------------------------------
            conn = _get_db()
            new_rows: List[Tuple[str, dict]] = []
            page_num = 0

            while page_num < 50:
                page_num += 1
                page_rows = await session.extract_grid_rows(frame)
                if not page_rows:
                    break

                new_on_page = 0
                for row in page_rows:
                    if len(row) < 10:
                        continue
                    msg_id = row[0].strip()
                    if not msg_id or not msg_id.isdigit():
                        continue
                    fax_id = f"nextiva_nmoyern_recv_{msg_id}"
                    if not _fax_exists(conn, fax_id):
                        new_on_page += 1

                logger.debug(
                    "Nextiva recv page check",
                    page=page_num, total=len(page_rows), new=new_on_page,
                )

                if new_on_page == 0 and not full_scrape:
                    logger.info(
                        "Nextiva recv early stop — all rows known",
                        stopped_at_page=page_num,
                    )
                    break

                for row in page_rows:
                    if len(row) < 10:
                        continue
                    message_id = row[0].strip()
                    if not message_id or not message_id.isdigit():
                        continue
                    fax_id = f"nextiva_nmoyern_recv_{message_id}"
                    if _fax_exists(conn, fax_id):
                        continue

                    csid_name = row[3].strip()
                    from_phone_raw = row[4].strip()
                    fax_date_str = row[5].strip()
                    pages = row[7].strip()
                    status = row[8].strip().lower()

                    mco = _csid_to_mco(csid_name)
                    from_number_digits = re.sub(r"[^\d]", "", from_phone_raw)
                    if len(from_number_digits) == 11 and from_number_digits.startswith("1"):
                        from_number_clean = from_number_digits[1:]
                    else:
                        from_number_clean = from_number_digits

                    # If CSID didn't resolve MCO, try the phone number as fallback
                if mco == "unknown" and from_number_clean:
                    mco_from_phone = fax_number_to_mco(from_number_clean)
                    if mco_from_phone != "unknown":
                        mco = mco_from_phone

                    company = mco if mco != "unknown" else ""
                    new_rows.append((fax_id, {
                        "message_id": message_id,
                        "csid_name": csid_name,
                        "from_number_clean": from_number_clean,
                        "fax_date": fax_date_str,
                        "fax_date_str": fax_date_str,
                        "pages": pages,
                        "status": status,
                        "mco": mco,
                        "company": company,
                    }))

                # Go to next page
                has_next = await session.click_next_page(frame)
                if not has_next:
                    break

            logger.info(
                "Nextiva received new rows to process",
                new_rows=len(new_rows),
            )

            if not new_rows:
                conn.close()
                return new_entries

            # Phase 2a: Insert ALL grid rows into DB immediately.
            # This ensures grid data (MCO, date, fax number) is never lost
            # even if PDF downloads fail or time out.
            for fax_id, row_data in new_rows:
                entry = {
                    "fax_id": fax_id,
                    "source": source,
                    "direction": "received",
                    "fax_date": row_data.get("fax_date_str", row_data.get("fax_date", "")),
                    "company": row_data.get("company", ""),
                    "mco": row_data.get("mco", ""),
                    "fax_number": row_data.get("from_number_clean", ""),
                    "client_name": "",
                    "auth_dates": "",
                    "auth_number": "",
                    "auth_status": "unknown",
                    "document_type": "unknown",
                    "reviewed_at": datetime.now().isoformat(),
                    "notes": (
                        f"Fax Status: {row_data['status']}"
                        + (f" | CSID: {row_data['csid_name']}" if row_data.get("csid_name") else "")
                    ),
                }

                if _insert_fax(conn, entry):
                    new_entries.append(entry)

            conn.commit()

            logger.info(
                "Nextiva received Phase 2a complete — grid data inserted",
                inserted=len(new_entries),
                total_new_rows=len(new_rows),
            )

            # Phase 2b: Download PDFs for received faxes (these are critical —
            # auth approvals/rejections only come via received fax PDFs).
            pdfs_downloaded = 0
            for fax_id, row_data in new_rows:
                if pdfs_downloaded >= MAX_PDF_DOWNLOADS_PER_RUN:
                    break

                message_id = row_data["message_id"]

                pdf_path = await _download_nextiva_pdf(
                    page, frame, message_id, "nmoyern", direction="recv"
                )
                if not pdf_path:
                    continue

                pdfs_downloaded += 1
                pdf_info = _extract_info_from_fax_pdf(pdf_path)
                logger.debug(
                    "Received fax PDF info extracted",
                    message_id=message_id,
                    client=pdf_info.get("client_name", ""),
                    auth=pdf_info.get("auth_number", ""),
                )

                # Read full text for document classification
                pdf_text = ""
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(pdf_path)
                    for pg in reader.pages:
                        pdf_text += (pg.extract_text() or "") + "\n"
                    if not pdf_text.strip():
                        pdf_text = _ocr_pdf(pdf_path)
                except Exception:
                    pdf_text = _ocr_pdf(pdf_path)

                # Classify document type and auth status from PDF content
                doc_type = _classify_received_document(
                    pdf_text, "", row_data["mco"], row_data["pages"]
                )
                auth_status = _classify_auth_status(pdf_text, "", doc_type)

                # Use PDF-extracted auth number if available, else try from text
                auth_number = pdf_info.get("auth_number", "")
                if not auth_number and pdf_text:
                    auth_number = _extract_auth_number(pdf_text)

                # Update the already-inserted DB record with PDF-extracted data
                conn.execute(
                    """UPDATE fax_log SET
                        client_name = ?,
                        company = CASE WHEN ? != '' THEN ? ELSE company END,
                        auth_dates = ?,
                        auth_number = ?,
                        auth_status = ?,
                        document_type = ?,
                        notes = ?
                    WHERE fax_id = ?""",
                    (
                        pdf_info.get("client_name", ""),
                        pdf_info.get("entity", ""), pdf_info.get("entity", ""),
                        pdf_info.get("auth_dates", ""),
                        auth_number,
                        auth_status,
                        doc_type,
                        (
                            f"Fax Status: {row_data['status']}"
                            + (f" | CSID: {row_data['csid_name']}" if row_data.get("csid_name") else "")
                            + (f" | Service: {pdf_info['service_type']}" if pdf_info.get("service_type") else "")
                            + (f" | Dx: {pdf_info['diagnosis']}" if pdf_info.get("diagnosis") else "")
                            + (f" | PDF: {pdf_path}" if pdf_path else "")
                        ),
                        fax_id,
                    ),
                )

                # Brief pause between downloads
                await asyncio.sleep(1.5)

            conn.commit()
            conn.close()

    except Exception as e:
        logger.error("Nextiva received fax scrape failed", error=str(e))

    logger.info(
        "Nextiva received fax scrape complete",
        new_count=len(new_entries),
        pdfs_downloaded=pdfs_downloaded if 'pdfs_downloaded' in dir() else 0,
    )
    return new_entries


# ---------------------------------------------------------------------------
# Document classification helpers
# ---------------------------------------------------------------------------

def _classify_subject(subject: str) -> str:
    """Classify document type from subject line."""
    if not subject:
        return "unknown"
    s = subject.lower()
    if any(kw in s for kw in ("sra", "service auth", "authorization request")):
        return "sra"
    if any(kw in s for kw in ("refax", "re-fax", "resubmit")):
        return "sra"
    if any(kw in s for kw in ("medical record", "clinical", "chart")):
        return "medical_records"
    if any(kw in s for kw in ("approval", "approved")):
        return "auth_approval"
    if any(kw in s for kw in ("denial", "denied", "reject")):
        return "auth_rejection"
    return "unknown"


def _classify_received_document(
    raw_text: str,
    subject: str,
    mco: str,
    pages: str,
) -> str:
    """
    Classify a received fax document type based on available metadata.
    """
    text = f"{raw_text} {subject}".lower()

    # Check for auth approval indicators
    if any(kw in text for kw in (
        "approved", "approval", "authorization approved",
        "auth approved", "approved through",
    )):
        return "auth_approval"

    # Check for rejection indicators
    if any(kw in text for kw in (
        "denied", "denial", "rejected", "not approved",
        "adverse determination", "adverse benefit",
    )):
        return "auth_rejection"

    # Check for medical records
    if any(kw in text for kw in (
        "medical record", "clinical", "chart note",
        "progress note", "assessment",
    )):
        return "medical_records"

    # Check for spam indicators
    if any(kw in text for kw in (
        "advertisement", "special offer", "discount",
        "marketing", "subscribe", "unsubscribe",
    )):
        return "spam"

    # If from a known MCO, likely auth-related
    if mco != "unknown":
        return "unknown"  # Could be auth or other MCO correspondence

    # Single page from unknown sender is often spam
    try:
        if pages and int(re.sub(r"[^\d]", "", pages)) <= 1 and mco == "unknown":
            return "spam"
    except (ValueError, TypeError):
        pass

    return "unknown"


def _classify_auth_status(raw_text: str, subject: str, doc_type: str) -> str:
    """Determine auth status from document classification."""
    if doc_type == "auth_approval":
        return "approved"
    if doc_type == "auth_rejection":
        return "rejected"
    if doc_type == "sra":
        return "submitted"
    return "unknown"


def _extract_auth_number(text: str) -> str:
    """Try to extract an authorization number from text."""
    if not text:
        return ""
    # Common auth number patterns
    patterns = [
        r"auth(?:orization)?\s*(?:#|number|num|no)?[:\s]*([A-Z0-9]{6,20})",
        r"(?:AUTH|PA|SA)\s*#?\s*([A-Z0-9]{6,20})",
        r"reference\s*(?:#|number)?[:\s]*([A-Z0-9]{6,20})",
    ]
    for pattern in patterns:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            return m.group(1)
    return ""


def _extract_client_name(subject: str) -> str:
    """Try to extract a client/patient name from the subject."""
    if not subject:
        return ""
    # Common patterns: "RE: John Smith Auth", "Patient: Smith, John"
    patterns = [
        r"(?:patient|client|member|re)\s*[:\s]+([A-Za-z]+[\s,]+[A-Za-z]+)",
        r"(?:for|regarding)\s+([A-Za-z]+\s+[A-Za-z]+)",
    ]
    for pattern in patterns:
        m = re.search(pattern, subject, re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


# ---------------------------------------------------------------------------
# 5. Fax Log Query Functions
# ---------------------------------------------------------------------------

def get_sent_fax_for_client(
    client_name: str,
    mco: Optional[str] = None,
    after_date: Optional[date] = None,
) -> List[dict]:
    """
    Search fax_log for sent faxes matching a client name (fuzzy match).
    Optionally filter by MCO and date.
    Returns matching fax entries.
    """
    conn = _get_db()

    query = "SELECT * FROM fax_log WHERE direction = 'sent'"
    params: list = []

    if mco:
        query += " AND (LOWER(mco) LIKE ? OR LOWER(company) LIKE ?)"
        mco_pattern = f"%{mco.lower()}%"
        params.extend([mco_pattern, mco_pattern])

    if after_date:
        query += " AND fax_date >= ?"
        params.append(after_date.isoformat())

    query += " ORDER BY fax_date DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    # Fuzzy-match client name in Python (SQLite LIKE isn't fuzzy enough)
    results = []
    for row in rows:
        row_dict = dict(row)
        row_client = row_dict.get("client_name", "")
        row_notes = row_dict.get("notes", "")

        if _fuzzy_match(client_name, row_client):
            results.append(row_dict)
        elif client_name:
            # Check if any part of the client name appears in notes
            name_parts = client_name.lower().split()
            if any(
                part in row_notes.lower()
                for part in name_parts
                if len(part) > 2
            ):
                results.append(row_dict)

    return results


def get_received_auth_for_client(
    client_name: str,
    mco: Optional[str] = None,
    skip_already_verified: bool = True,
) -> List[dict]:
    """
    Search fax_log for received faxes that might be auth approvals/rejections.
    Skips entries already verified for entity (entity_verified=1) unless
    skip_already_verified=False.
    Returns matching entries.
    """
    conn = _get_db()

    query = """
        SELECT * FROM fax_log
        WHERE direction = 'received'
          AND document_type IN ('auth_approval', 'auth_rejection', 'unknown')
    """
    params: list = []

    if skip_already_verified:
        query += " AND entity_verified = 0"

    if mco:
        query += " AND (LOWER(mco) LIKE ? OR LOWER(company) LIKE ?)"
        mco_pattern = f"%{mco.lower()}%"
        params.extend([mco_pattern, mco_pattern])

    query += " ORDER BY fax_date DESC"
    rows = conn.execute(query, params).fetchall()
    conn.close()

    # Fuzzy-match client name
    results = []
    for row in rows:
        row_dict = dict(row)
        row_client = row_dict.get("client_name", "")
        row_notes = row_dict.get("notes", "")

        if _fuzzy_match(client_name, row_client):
            results.append(row_dict)
        elif client_name:
            name_parts = client_name.lower().split()
            if any(
                part in (row_notes + " " + row_client).lower()
                for part in name_parts
                if len(part) > 2
            ):
                results.append(row_dict)

    return results


def mark_fax_entity_verified(
    fax_id: str,
    verified_for_client: str = "",
) -> None:
    """
    Mark a fax_log entry as verified for entity purposes.
    Prevents re-checking the same fax on future runs.
    """
    conn = _get_db()
    conn.execute(
        """UPDATE fax_log
           SET entity_verified = 1,
               entity_verified_at = ?,
               entity_verified_for = ?
           WHERE fax_id = ?""",
        (datetime.now().isoformat(), verified_for_client, fax_id),
    )
    conn.commit()
    conn.close()
    logger.debug(
        "Fax marked as entity-verified",
        fax_id=fax_id,
        client=verified_for_client,
    )


def get_fax_log_summary(days: int = 30) -> dict:
    """Get a summary of fax activity over the last N days."""
    conn = _get_db()
    cutoff = (datetime.now() - timedelta(days=days)).isoformat()

    summary = {
        "total": 0,
        "sent": 0,
        "received": 0,
        "by_source": {},
        "by_mco": {},
        "by_type": {},
        "approvals": 0,
        "rejections": 0,
        "spam": 0,
    }

    rows = conn.execute(
        "SELECT * FROM fax_log WHERE reviewed_at >= ?", (cutoff,)
    ).fetchall()
    conn.close()

    for row in rows:
        r = dict(row)
        summary["total"] += 1

        if r["direction"] == "sent":
            summary["sent"] += 1
        else:
            summary["received"] += 1

        source = r.get("source", "unknown")
        summary["by_source"][source] = summary["by_source"].get(source, 0) + 1

        mco = r.get("mco", "unknown")
        if mco:
            summary["by_mco"][mco] = summary["by_mco"].get(mco, 0) + 1

        doc_type = r.get("document_type", "unknown")
        summary["by_type"][doc_type] = summary["by_type"].get(doc_type, 0) + 1

        if doc_type == "auth_approval":
            summary["approvals"] += 1
        elif doc_type == "auth_rejection":
            summary["rejections"] += 1
        elif doc_type == "spam":
            summary["spam"] += 1

    return summary


# ---------------------------------------------------------------------------
# 6. Full Refresh — scrape all 4 sources
# ---------------------------------------------------------------------------

async def refresh_all_fax_sources(
    lauris_session: Optional[LaurisSession] = None,
    lookback_days: int = 30,
) -> dict:
    """
    Scrape all 4 fax sources and insert new entries into fax_log.

    Sources:
      1. Lauris sent fax history
      2. Nextiva nmoyern sent
      3. Nextiva nmoyern2 sent
      4. Nextiva nmoyern received

    Args:
        lauris_session: Existing LaurisSession (will create one if None)
        lookback_days: How far back to look if no previous scrape date

    Returns dict with counts per source.
    """
    results = {
        "lauris_sent": 0,
        "nextiva_nmoyern_sent": 0,
        "nextiva_nmoyern2_sent": 0,
        "nextiva_nmoyern_received": 0,
        "errors": [],
    }

    default_start = date.today() - timedelta(days=lookback_days)

    # 1. Lauris fax history
    try:
        last = get_last_reviewed_date("lauris_sent")
        start = last.date() if last else default_start

        if lauris_session:
            new = await scrape_lauris_fax_history(lauris_session, start)
        else:
            async with LaurisSession() as ls:
                new = await scrape_lauris_fax_history(ls, start)
        results["lauris_sent"] = len(new)
    except Exception as e:
        logger.error("Lauris fax scrape failed in refresh", error=str(e))
        results["errors"].append(f"lauris_sent: {str(e)[:100]}")

    # 2 & 3. Nextiva sent faxes (both accounts, can run in parallel)
    try:
        nmoyern_task = scrape_nextiva_sent_faxes("nmoyern")
        nmoyern2_task = scrape_nextiva_sent_faxes("nmoyern2")
        nmoyern_new, nmoyern2_new = await asyncio.gather(
            nmoyern_task, nmoyern2_task, return_exceptions=True
        )

        if isinstance(nmoyern_new, list):
            results["nextiva_nmoyern_sent"] = len(nmoyern_new)
        else:
            results["errors"].append(f"nextiva_nmoyern_sent: {str(nmoyern_new)[:100]}")

        if isinstance(nmoyern2_new, list):
            results["nextiva_nmoyern2_sent"] = len(nmoyern2_new)
        else:
            results["errors"].append(f"nextiva_nmoyern2_sent: {str(nmoyern2_new)[:100]}")
    except Exception as e:
        logger.error("Nextiva sent fax scrape failed", error=str(e))
        results["errors"].append(f"nextiva_sent: {str(e)[:100]}")

    # 4. Nextiva received faxes
    try:
        recv_new = await scrape_nextiva_received_faxes()
        results["nextiva_nmoyern_received"] = len(recv_new)
    except Exception as e:
        logger.error("Nextiva received fax scrape failed", error=str(e))
        results["errors"].append(f"nextiva_received: {str(e)[:100]}")

    total_new = sum(v for k, v in results.items() if k != "errors")
    logger.info(
        "All fax sources refreshed",
        total_new=total_new,
        details=results,
    )
    return results


# ---------------------------------------------------------------------------
# 7. Integration helper — fax_log lookup for auth verification cascade
# ---------------------------------------------------------------------------

async def check_fax_log_for_auth(
    client_name: str,
    mco: str,
    claim_dos: Optional[date] = None,
    auto_refresh: bool = True,
) -> dict:
    """
    Check the fax_log for auth-related faxes for a client.
    Used by handle_mco_auth_check() in handlers.py.

    Cascade:
      1. Check fax_log for sent faxes (was auth faxed?)
      2. Check fax_log for received auth responses (approval/rejection?)
      3. If nothing found and auto_refresh=True, trigger a full scrape
      4. Re-check after scrape

    Returns:
        {
            "sent_found": bool,
            "sent_entries": list,
            "received_found": bool,
            "received_entries": list,
            "auth_approved": bool,
            "auth_number": str,
            "auth_rejected": bool,
            "refreshed": bool,
        }
    """
    result = {
        "sent_found": False,
        "sent_entries": [],
        "received_found": False,
        "received_entries": [],
        "auth_approved": False,
        "auth_number": "",
        "auth_rejected": False,
        "refreshed": False,
    }

    # Step 1: Check for sent faxes
    sent = get_sent_fax_for_client(
        client_name,
        mco=mco,
        after_date=claim_dos - timedelta(days=30) if claim_dos else None,
    )
    if sent:
        result["sent_found"] = True
        result["sent_entries"] = sent

    # Step 2: Check for received auth responses
    received = get_received_auth_for_client(client_name, mco=mco)
    if received:
        result["received_found"] = True
        result["received_entries"] = received

        # Check for approvals
        for entry in received:
            if entry.get("document_type") == "auth_approval":
                result["auth_approved"] = True
                if entry.get("auth_number"):
                    result["auth_number"] = entry["auth_number"]
                break

        # Check for rejections
        for entry in received:
            if entry.get("document_type") == "auth_rejection":
                result["auth_rejected"] = True
                break

    # Step 3: If nothing found, trigger refresh
    if not sent and not received and auto_refresh:
        logger.info(
            "No fax records found — triggering full refresh",
            client=client_name,
            mco=mco,
        )
        await refresh_all_fax_sources()
        result["refreshed"] = True

        # Re-check after refresh
        sent = get_sent_fax_for_client(
            client_name,
            mco=mco,
            after_date=claim_dos - timedelta(days=30) if claim_dos else None,
        )
        if sent:
            result["sent_found"] = True
            result["sent_entries"] = sent

        received = get_received_auth_for_client(client_name, mco=mco)
        if received:
            result["received_found"] = True
            result["received_entries"] = received
            for entry in received:
                if entry.get("document_type") == "auth_approval":
                    result["auth_approved"] = True
                    if entry.get("auth_number"):
                        result["auth_number"] = entry["auth_number"]
                    break
            for entry in received:
                if entry.get("document_type") == "auth_rejection":
                    result["auth_rejected"] = True
                    break

    return result


# ------------------------------------------------------------------
# 5. Gmail-based fax scrapers
# ------------------------------------------------------------------

GMAIL_PDF_DIR = Path("/tmp/fax_downloads/gmail")
GMAIL_PDF_DIR.mkdir(parents=True, exist_ok=True)


def _imap_login(
    email_addr: str, password: str
) -> imaplib.IMAP4_SSL:
    """Login to Gmail IMAP."""
    mail = imaplib.IMAP4_SSL("imap.gmail.com")
    mail.login(email_addr, password)
    return mail


def _process_email_pdf(
    msg, fax_id: str, source: str, direction: str
) -> Optional[dict]:
    """
    Download PDF from email, extract ALL data from the PDF
    (not from email metadata), return fax_log entry.
    """
    fax_date = (msg["Date"] or "")[:30].strip()

    pdf_path = ""
    for part in msg.walk():
        fn = part.get_filename()
        if not fn:
            continue
        if fn.lower().endswith((".pdf", ".tif", ".tiff")):
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            safe = re.sub(r"[^\w.\-]", "_", fn)[:60]
            pdf_path = str(GMAIL_PDF_DIR / safe)
            with open(pdf_path, "wb") as f:
                f.write(payload)
            break

    if not pdf_path:
        return None

    info = _extract_info_from_fax_pdf(pdf_path)
    scan_ts = datetime.now().isoformat()

    notes_parts = [f"PDF: {pdf_path}"]
    if info.get("service_type"):
        notes_parts.append(f"Service: {info['service_type']}")
    if info.get("diagnosis"):
        notes_parts.append(f"Dx: {info['diagnosis']}")
    if info.get("auth_response"):
        notes_parts.append(
            f"auth_response: {info['auth_response']}"
        )

    auth_status = "unknown"
    if info.get("auth_response") == "denied":
        auth_status = "denied"
    elif info.get("auth_response") == "approved":
        auth_status = "approved"
    elif direction == "sent":
        auth_status = "submitted"

    return {
        "fax_id": fax_id,
        "source": source,
        "direction": direction,
        "fax_date": fax_date,
        "company": info.get("entity", ""),
        "mco": "",
        "fax_number": "",
        "client_name": info.get("client_name", ""),
        "auth_dates": info.get("auth_dates", ""),
        "auth_number": info.get("auth_number", ""),
        "auth_status": auth_status,
        "document_type": info.get("service_type", "unknown"),
        "scan_completed": scan_ts,
        "notes": " | ".join(notes_parts),
    }


async def scrape_gmail_sent_faxes(
    since_date: Optional[date] = None,
    max_pdfs: int = 9999,
) -> List[dict]:
    """
    Scrape sent faxes from fax@lifeconsultantsinc.org
    Sent Mail. Downloads PDF attachments and extracts all
    data from the PDF.
    """
    email_addr = os.getenv("FAX_EMAIL", "")
    password = os.getenv("FAX_EMAIL_PASSWORD", "")
    if not email_addr or not password:
        logger.warning("FAX_EMAIL credentials not set")
        return []

    if since_date is None:
        since_date = date.today() - timedelta(days=210)

    results: List[dict] = []
    conn = _get_db()

    try:
        mail = _imap_login(email_addr, password)
        mail.select('"[Gmail]/Sent Mail"')

        since_str = since_date.strftime("%d-%b-%Y")
        status, data = mail.search(
            None, f"SINCE {since_str}"
        )
        all_ids = data[0].split()
        logger.info(
            "Gmail sent emails found",
            count=len(all_ids), since=since_str,
        )

        downloaded = 0
        skipped = 0
        for eid in reversed(all_ids):
            if downloaded >= max_pdfs:
                break

            status, hdr = mail.fetch(
                eid,
                "(BODY[HEADER.FIELDS (MESSAGE-ID)])",
            )
            hdr_msg = email_lib.message_from_bytes(
                hdr[0][1]
            )
            msg_id = hdr_msg["Message-ID"] or eid.decode()
            fax_id = (
                f"gmail_sent_"
                f"{hash(msg_id) & 0xFFFFFFFF:08x}"
            )

            if _fax_exists(conn, fax_id):
                skipped += 1
                continue

            status, msg_data = mail.fetch(
                eid, "(RFC822)"
            )
            msg = email_lib.message_from_bytes(
                msg_data[0][1]
            )

            entry = _process_email_pdf(
                msg, fax_id, "gmail_sent", "sent"
            )
            if entry and _insert_fax(conn, entry):
                conn.commit()
                results.append(entry)
                downloaded += 1
                if downloaded % 10 == 0:
                    logger.info(
                        "Gmail sent progress",
                        downloaded=downloaded,
                        named=sum(
                            1 for r in results
                            if r.get("client_name")
                        ),
                    )

        mail.logout()
        logger.info(
            "Gmail sent scrape complete",
            new_count=len(results), skipped=skipped,
        )

    except Exception as e:
        logger.error(
            "Gmail sent scrape failed", error=str(e)
        )

    conn.close()
    return results


async def scrape_gmail_received_faxes(
    since_date: Optional[date] = None,
    max_pdfs: int = 9999,
) -> List[dict]:
    """
    Scrape received faxes from admin@lifeconsultantsinc.org.
    Filters inbox for emails from nextivafax.com.
    Downloads PDF and extracts all data from PDF.
    """
    email_addr = os.getenv("ADMIN_EMAIL", "")
    password = os.getenv("ADMIN_EMAIL_PASSWORD", "")
    if not email_addr or not password:
        logger.warning("ADMIN_EMAIL credentials not set")
        return []

    if since_date is None:
        since_date = date.today() - timedelta(days=210)

    results: List[dict] = []
    conn = _get_db()

    try:
        mail = _imap_login(email_addr, password)
        mail.select("INBOX")

        since_str = since_date.strftime("%d-%b-%Y")
        status, data = mail.search(
            None,
            f'FROM "nextivafax.com" SINCE {since_str}',
        )
        all_ids = data[0].split()
        logger.info(
            "Gmail received faxes found",
            count=len(all_ids), since=since_str,
        )

        downloaded = 0
        skipped = 0
        for eid in reversed(all_ids):
            if downloaded >= max_pdfs:
                break

            status, hdr = mail.fetch(
                eid,
                "(BODY[HEADER.FIELDS (MESSAGE-ID SUBJECT)])",
            )
            hdr_msg = email_lib.message_from_bytes(
                hdr[0][1]
            )
            subj = hdr_msg["Subject"] or ""
            msg_id = hdr_msg["Message-ID"] or eid.decode()
            fax_id = (
                f"gmail_recv_"
                f"{hash(msg_id) & 0xFFFFFFFF:08x}"
            )

            if _fax_exists(conn, fax_id):
                skipped += 1
                continue

            # Skip send confirmations (no PDF attached)
            if "Message Sent:" in subj:
                skipped += 1
                continue

            status, msg_data = mail.fetch(
                eid, "(RFC822)"
            )
            msg = email_lib.message_from_bytes(
                msg_data[0][1]
            )

            entry = _process_email_pdf(
                msg, fax_id, "gmail_received", "received"
            )
            if entry and _insert_fax(conn, entry):
                conn.commit()
                results.append(entry)
                downloaded += 1
                if downloaded % 10 == 0:
                    logger.info(
                        "Gmail received progress",
                        downloaded=downloaded,
                        named=sum(
                            1 for r in results
                            if r.get("client_name")
                        ),
                    )

        mail.logout()
        logger.info(
            "Gmail received scrape complete",
            new_count=len(results), skipped=skipped,
        )

    except Exception as e:
        logger.error(
            "Gmail received scrape failed",
            error=str(e),
        )

    conn.close()
    return results
