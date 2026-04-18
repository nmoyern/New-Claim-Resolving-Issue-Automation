"""
config/settings.py
------------------
Central config loader. Reads .env + pulls live credentials from the
Admin Logins Google Sheet so nothing sensitive is hardcoded.
"""
from __future__ import annotations

import os
import json
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Optional

from dotenv import load_dotenv

from config.entities import KJLN, MARYS_HOME, NHCS

def _load_environment_files() -> None:
    """Load the project env first, then known local fallback env files."""
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")
    load_dotenv(Path.home() / "availity-test" / ".env", override=False)


_load_environment_files()


# ---------------------------------------------------------------------------
# Org constants
# ---------------------------------------------------------------------------
MARYS_HOME_NPI    = os.getenv("MARYS_HOME_NPI", MARYS_HOME.billing_npi)
MARYS_HOME_TAX_ID = os.getenv("MARYS_HOME_TAX_ID", MARYS_HOME.tax_id)
ORG_MARYS_HOME    = os.getenv("ORG_NAME_MARYS_HOME", MARYS_HOME.claimmd_region)
ORG_KJLN          = os.getenv("ORG_NAME_KJLN", KJLN.claimmd_region)
ORG_NHCS          = os.getenv("ORG_NAME_NHCS", NHCS.claimmd_region)

# Rendering provider for Mary's Home Anthem claims
DR_YANCEY_NAME = os.getenv("DR_YANCEY_NAME", "Tiffinee Yancey")
DR_YANCEY_NPI  = os.getenv("DR_YANCEY_NPI", "1619527645")

AUTOMATION_INITIALS = os.getenv("AUTOMATION_INITIALS", "AUTO")
DRY_RUN             = os.getenv("DRY_RUN", "false").lower() == "true"
MAX_CLAIMS_PER_RUN  = int(os.getenv("MAX_CLAIMS_PER_RUN", "200"))
SKIP_NEWER_DAYS     = int(os.getenv("SKIP_CLAIMS_NEWER_THAN_DAYS", "7"))
LOG_LEVEL           = os.getenv("LOG_LEVEL", "INFO")
LOG_DIR             = Path(os.getenv("LOG_DIR", "./logs"))
SESSION_DIR         = Path(os.getenv("SESSION_DIR", "./sessions"))
HUMAN_REVIEW_EMAIL  = os.getenv("HUMAN_REVIEW_EMAIL", "nm@lifeconsultantsinc.org")

# Sheet IDs
ADMIN_LOGINS_SHEET_ID       = os.getenv("ADMIN_LOGINS_SHEET_ID", "1vxgGaJPSk5R6RM7PgHaI1xC0e37CzhWFQocblB7sWxQ")
CLAIM_DENIAL_CALLS_SHEET_ID = os.getenv("CLAIM_DENIAL_CALLS_SHEET_ID", "1yq4vLBeFSpMPun5nou77fAiRppROgNzk2Bkh9eL1mjc")
CONT_STAYS_SHEET_ID         = os.getenv("CONT_STAYS_SHEET_ID", "1S_7jo1CmrkVcZRCSNZvWEl4LqAq5zQqQaiUWuljVH5s")

# ClickUp
CLICKUP_API_TOKEN      = os.getenv("CLICKUP_API_TOKEN", "")
CLICKUP_DAILY_TASK_ID  = os.getenv("CLICKUP_DAILY_TASK_ID", "86ad83kf7")
CLICKUP_WORKSPACE_ID   = os.getenv("CLICKUP_WORKSPACE_ID", "36102551")
CLICKUP_LIST_ID        = os.getenv("CLICKUP_LIST_ID", "")

# Nextiva Fax
NEXTIVA_FAX_URL      = os.getenv("NEXTIVA_FAX_URL", "https://fax.nextiva.com/xauth/")
NEXTIVA_FAX_USERNAME = os.getenv("NEXTIVA_FAX_USERNAME", "NMoyerN")
NEXTIVA_FAX_PASSWORD = os.getenv("NEXTIVA_FAX_PASSWORD", "")

# Power BI
POWERBI_WORKSPACE_ID = os.getenv("POWERBI_WORKSPACE_ID", "8d724e00-8c1d-4d3c-b804-86c163a258c5")
POWERBI_REPORT_ID    = os.getenv("POWERBI_REPORT_ID", "39dcf41c-1d1b-428a-9086-a8c7e1f3c0f8")
POWERBI_EMAIL        = os.getenv("POWERBI_EMAIL", "nm@lifeconsultantsinc.org")
POWERBI_PASSWORD     = os.getenv("POWERBI_PASSWORD", "")
POWERBI_REPORT_URL   = os.getenv("POWERBI_REPORT_URL", "")


# ---------------------------------------------------------------------------
# Credential dataclasses
# ---------------------------------------------------------------------------

@dataclass
class PortalCredentials:
    username: str
    password: str
    url: str
    mfa_type: str = "none"        # "none" | "duo_push" | "totp" | "manual"
    fax_number: str = ""
    extra: dict = field(default_factory=dict)


@dataclass
class AllCredentials:
    claimmd:   PortalCredentials = None
    lauris:    PortalCredentials = None
    sentara:   PortalCredentials = None
    united:    PortalCredentials = None
    availity:  PortalCredentials = None   # covers Molina, Anthem, Aetna
    kepro:     PortalCredentials = None
    nextiva:   PortalCredentials = None
    google_sa: Optional[dict] = None      # service account JSON dict


# ---------------------------------------------------------------------------
# Live credential loader from Admin Logins Google Sheet
# ---------------------------------------------------------------------------

class CredentialLoader:
    """
    Reads the Admin Logins Google Sheet (tab: 'Portal Logins') and maps
    portal names to PortalCredentials objects.

    Expected sheet columns:
        A: Portal Name   B: URL   C: Username   D: Password
        E: MFA Type      F: Fax Number   G: Notes
    """

    PORTAL_ROW_MAP = {
        "claimmd":  "Claim.MD",
        "lauris":   "Lauris",
        "sentara":  "Sentara",
        "united":   "United / UHC",
        "availity": "Availity",
        "kepro":    "Kepro / Atrezzo",
    }

    def __init__(self):
        self._creds: Optional[AllCredentials] = None
        sa_path = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
        if sa_path and Path(sa_path).exists():
            with open(sa_path) as f:
                self._sa_json = json.load(f)
        else:
            self._sa_json = None

    def load(self) -> AllCredentials:
        if self._creds is not None:
            return self._creds

        if self._sa_json is None:
            # Fallback: read from environment variables directly
            return self._load_from_env()

        try:
            return self._load_from_sheets()
        except Exception as e:
            print(f"[CredentialLoader] Google Sheets unavailable ({e}), falling back to .env")
            return self._load_from_env()

    def _load_from_env(self) -> AllCredentials:
        """Fallback: read credentials from environment variables."""
        return AllCredentials(
            claimmd=PortalCredentials(
                username=os.getenv("CLAIMMD_USERNAME", ""),
                password=os.getenv("CLAIMMD_PASSWORD", ""),
                url=os.getenv("CLAIMMD_URL", "https://www.claim.md/"),
            ),
            lauris=PortalCredentials(
                username=os.getenv("LAURIS_USERNAME", ""),
                password=os.getenv("LAURIS_PASSWORD", ""),
                url=os.getenv("LAURIS_URL", ""),
            ),
            sentara=PortalCredentials(
                username=os.getenv("SENTARA_USERNAME", ""),
                password=os.getenv("SENTARA_PASSWORD", ""),
                url=os.getenv("SENTARA_URL", "https://apps.sentarahealthplans.com/providers/login/login.aspx"),
                mfa_type=os.getenv("SENTARA_MFA_TYPE", "duo_push"),
            ),
            united=PortalCredentials(
                username=os.getenv("UNITED_USERNAME", ""),
                password=os.getenv("UNITED_PASSWORD", ""),
                url=os.getenv("UNITED_URL", "https://www.uhcprovider.com/"),
            ),
            availity=PortalCredentials(
                username=os.getenv("AVAILITY_USERNAME", ""),
                password=os.getenv("AVAILITY_PASSWORD", ""),
                url=os.getenv("AVAILITY_URL", "https://apps.availity.com/"),
            ),
            kepro=PortalCredentials(
                username=os.getenv("KEPRO_USERNAME", ""),
                password=os.getenv("KEPRO_PASSWORD", ""),
                url=os.getenv("KEPRO_URL", "https://portal.kepro.com/Home/Index"),
            ),
            nextiva=PortalCredentials(
                username=NEXTIVA_FAX_USERNAME,
                password=NEXTIVA_FAX_PASSWORD,
                url=NEXTIVA_FAX_URL,
            ),
            google_sa=self._sa_json,
        )

    def _load_from_sheets(self) -> AllCredentials:
        """Load live credentials from the Admin Logins Google Sheet."""
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds = Credentials.from_service_account_info(self._sa_json, scopes=scopes)
        gc = gspread.authorize(creds)
        sheet = gc.open_by_key(ADMIN_LOGINS_SHEET_ID)
        ws = sheet.worksheet("Portal Logins")
        rows = ws.get_all_records()  # [{Portal Name, URL, Username, Password, ...}]

        portal_map = {}
        for row in rows:
            name = str(row.get("Portal Name", "")).strip()
            for key, expected in self.PORTAL_ROW_MAP.items():
                if expected.lower() in name.lower():
                    portal_map[key] = PortalCredentials(
                        username=row.get("Username", ""),
                        password=row.get("Password", ""),
                        url=row.get("URL", ""),
                        mfa_type=row.get("MFA Type", "none").lower(),
                        fax_number=row.get("Fax Number", ""),
                    )

        return AllCredentials(
            claimmd=portal_map.get("claimmd"),
            lauris=portal_map.get("lauris"),
            sentara=portal_map.get("sentara"),
            united=portal_map.get("united"),
            availity=portal_map.get("availity"),
            kepro=portal_map.get("kepro"),
            nextiva=PortalCredentials(
                username=NEXTIVA_FAX_USERNAME,
                password=NEXTIVA_FAX_PASSWORD,
                url=NEXTIVA_FAX_URL,
            ),
            google_sa=self._sa_json,
        )


@lru_cache(maxsize=1)
def get_credentials() -> AllCredentials:
    return CredentialLoader().load()

# Bank portals
BANK_WELLSFARGO_URL      = os.getenv("BANK_WELLSFARGO_URL", "https://www.wellsfargo.com")
BANK_WELLSFARGO_USERNAME = os.getenv("BANK_WELLSFARGO_USERNAME", "")
BANK_WELLSFARGO_PASSWORD = os.getenv("BANK_WELLSFARGO_PASSWORD", "")

BANK_SOUTHERN_URL        = os.getenv("BANK_SOUTHERN_URL", "https://southernbank.ebanking-services.com/eAM/Credential/Index?appId=beb&brand=southernbank")
BANK_SOUTHERN_COMPANY_ID = os.getenv("BANK_SOUTHERN_COMPANY_ID", "")
BANK_SOUTHERN_USERNAME   = os.getenv("BANK_SOUTHERN_USERNAME", "")
BANK_SOUTHERN_PASSWORD   = os.getenv("BANK_SOUTHERN_PASSWORD", "")

BANK_BOA_URL             = os.getenv("BANK_BOA_URL", "https://www.bankofamerica.com/business/")
BANK_BOA_USERNAME        = os.getenv("BANK_BOA_USERNAME", "")
BANK_BOA_PASSWORD        = os.getenv("BANK_BOA_PASSWORD", "")

# Azure AD / Power BI
AZURE_CLIENT_ID     = os.getenv("AZURE_CLIENT_ID", "")
AZURE_CLIENT_SECRET = os.getenv("AZURE_CLIENT_SECRET", "")
AZURE_TENANT_ID     = os.getenv("AZURE_TENANT_ID", "")
