"""
tools/selector_audit.py
------------------------
Audits all CSS/text selectors used in portal automation against live pages.
Run this when portal UIs change to identify which selectors need updating
before a full production run.

Usage:
  python tools/selector_audit.py --portal claimmd
  python tools/selector_audit.py --portal all
  python tools/selector_audit.py --portal lauris --headless false
"""
from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Dict, List, Optional

# Ensure project root is on path
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from sources.browser_base import BrowserSession
from sources.claimmd import SELECTORS as CLAIMMD_SELECTORS
from config.settings import get_credentials
from logging_utils.logger import get_logger, setup_logging

logger = get_logger("selector_audit")
REPORT_DIR = Path("/tmp/claims_selector_audit")
REPORT_DIR.mkdir(parents=True, exist_ok=True)


@dataclass
class SelectorResult:
    portal: str
    selector_name: str
    selector_value: str
    found: bool
    page_url: str
    error: Optional[str] = None
    screenshot: Optional[str] = None


@dataclass
class AuditReport:
    portal: str
    run_date: str = field(default_factory=lambda: date.today().isoformat())
    results: List[SelectorResult] = field(default_factory=list)

    @property
    def pass_count(self) -> int:
        return sum(1 for r in self.results if r.found)

    @property
    def fail_count(self) -> int:
        return sum(1 for r in self.results if not r.found)

    def print_summary(self):
        print(f"\n{'='*60}")
        print(f"SELECTOR AUDIT: {self.portal.upper()}  [{self.run_date}]")
        print(f"{'='*60}")
        print(f"  PASSED: {self.pass_count}")
        print(f"  FAILED: {self.fail_count}")
        if self.fail_count:
            print(f"\n  BROKEN SELECTORS:")
            for r in self.results:
                if not r.found:
                    print(f"    ✗  [{r.selector_name}]")
                    print(f"       Selector: {r.selector_value}")
                    print(f"       URL:      {r.page_url}")
                    if r.error:
                        print(f"       Error:    {r.error}")
        print(f"{'='*60}\n")

    def save(self) -> str:
        path = str(REPORT_DIR / f"audit_{self.portal}_{self.run_date}.json")
        data = {
            "portal":     self.portal,
            "run_date":   self.run_date,
            "pass_count": self.pass_count,
            "fail_count": self.fail_count,
            "results": [
                {
                    "selector_name":  r.selector_name,
                    "selector_value": r.selector_value,
                    "found":          r.found,
                    "page_url":       r.page_url,
                    "error":          r.error,
                    "screenshot":     r.screenshot,
                }
                for r in self.results
            ],
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)
        print(f"  Report saved: {path}")
        return path


# ---------------------------------------------------------------------------
# Auditors per portal
# ---------------------------------------------------------------------------

class ClaimMDAuditor(BrowserSession):
    SESSION_NAME = "claimmd_audit"

    def __init__(self, headless: bool = True):
        super().__init__(headless=headless)
        self._creds = get_credentials().claimmd

    @property
    def login_url(self) -> str:
        return self._creds.url if self._creds else "https://www.claim.md/"

    async def _is_logged_in(self) -> bool:
        try:
            el = await self.page.query_selector(CLAIMMD_SELECTORS["logged_in_marker"])
            return el is not None
        except Exception:
            return False

    async def _perform_login(self) -> bool:
        if not self._creds or not self._creds.username:
            logger.warning("No Claim.MD credentials — audit will test pre-login selectors only")
            return False
        await self.page.goto(self.login_url, wait_until="domcontentloaded")
        try:
            await self.safe_fill(CLAIMMD_SELECTORS["username_field"], self._creds.username)
            await self.safe_fill(CLAIMMD_SELECTORS["password_field"], self._creds.password)
            await self.safe_click(CLAIMMD_SELECTORS["login_button"])
            import asyncio
            await asyncio.sleep(2)
        except Exception as e:
            logger.warning("Login attempt failed during audit", error=str(e))
        return await self._is_logged_in()

    async def audit(self) -> AuditReport:
        report = AuditReport(portal="claimmd")
        import asyncio

        # --- Pre-login selectors ---
        await self.page.goto(self.login_url, wait_until="domcontentloaded")
        pre_login = ["username_field", "password_field", "login_button"]
        for name in pre_login:
            sel = CLAIMMD_SELECTORS[name]
            result = await self._probe(name, sel, self.login_url)
            report.results.append(result)

        # --- Post-login selectors (only if we have credentials) ---
        if self._creds and self._creds.username:
            await self._do_login()
            if await self._is_logged_in():
                post_login = [
                    "logged_in_marker", "manage_claims_link", "denied_tab",
                    "other_actions", "notes_field",
                ]
                for name in post_login:
                    sel = CLAIMMD_SELECTORS[name]
                    result = await self._probe(name, sel, self.page.url)
                    report.results.append(result)
        return report

    async def _probe(self, name: str, selector: str, url: str) -> SelectorResult:
        """Try to find a selector on the current page."""
        try:
            # Try each comma-separated alternative
            for part in selector.split(","):
                part = part.strip()
                el = await self.page.query_selector(part)
                if el:
                    return SelectorResult(
                        portal="claimmd", selector_name=name,
                        selector_value=selector, found=True, page_url=url,
                    )
            # None found — take a screenshot
            shot_path = str(REPORT_DIR / f"missing_{name}_{date.today().isoformat()}.png")
            await self.screenshot(f"missing_{name}")
            return SelectorResult(
                portal="claimmd", selector_name=name, selector_value=selector,
                found=False, page_url=url, screenshot=shot_path,
            )
        except Exception as e:
            return SelectorResult(
                portal="claimmd", selector_name=name, selector_value=selector,
                found=False, page_url=url, error=str(e),
            )


class LaurisAuditor(BrowserSession):
    """Audits Lauris selectors. Requires valid Lauris URL + credentials."""
    SESSION_NAME = "lauris_audit"

    # Key selectors to verify (mirrors lauris/billing.py)
    SELECTORS = {
        # Navigation
        "billing_center":   "a:has-text('Billing'), a[href*='billing']",
        "applications_menu":"a:has-text('Applications'), button:has-text('Applications')",
        "fax_proxy":        "a:has-text('Faxing Proxy'), a:has-text('Fax')",
        # Client search
        "client_search":    "input[name*='search'], input[id*='client_search']",
        # Fax reports
        "fax_history_tab":  "a:has-text('Fax History Report')",
        "fax_status_tab":   "a:has-text('Fax Status Report')",
        "fax_resend_btn":   "button:has-text('Re-Send'), input[value*='Re-Send']",
        # ERA
        "era_upload":       "a:has-text('ERA'), a[href*='era'], a[href*='835']",
        "file_input":       "input[type='file']",
        # Write-off
        "writeoff_btn":     "a:has-text('Write Off'), button:has-text('Write Off')",
        # Billing
        "submit_billing":   "button:has-text('Submit Billing'), button:has-text('Submit')",
        "double_billing":   "a:has-text('Double Billing'), text='Double Billing Report'",
    }

    def __init__(self, headless: bool = True):
        super().__init__(headless=headless)
        self._creds = get_credentials().lauris

    @property
    def login_url(self) -> str:
        return self._creds.url if self._creds else ""

    async def _is_logged_in(self) -> bool:
        try:
            el = await self.page.query_selector(".logout, a[href*='logout'], #home-menu")
            return el is not None
        except Exception:
            return False

    async def _perform_login(self) -> bool:
        if not self.login_url:
            return False
        await self.page.goto(self.login_url)
        try:
            await self.safe_fill("input[name='username']", self._creds.username)
            await self.safe_fill("input[type='password']", self._creds.password)
            await self.safe_click("button[type='submit']")
            import asyncio
            await asyncio.sleep(2)
        except Exception:
            pass
        return await self._is_logged_in()

    async def audit(self) -> AuditReport:
        report = AuditReport(portal="lauris")
        if not self.login_url:
            logger.warning("Lauris URL not configured — skipping audit")
            return report

        if not await self._is_logged_in():
            r = SelectorResult(
                portal="lauris", selector_name="LOGIN",
                selector_value="login", found=False,
                page_url=self.login_url,
                error="Login failed — cannot audit post-login selectors",
            )
            report.results.append(r)
            return report

        import asyncio
        for name, sel in self.SELECTORS.items():
            found = False
            error = None
            try:
                for part in sel.split(","):
                    el = await self.page.query_selector(part.strip())
                    if el:
                        found = True
                        break
            except Exception as e:
                error = str(e)
            report.results.append(SelectorResult(
                portal="lauris", selector_name=name, selector_value=sel,
                found=found, page_url=self.page.url, error=error,
            ))
            await asyncio.sleep(0.2)

        return report


class MCOPortalAuditor(BrowserSession):
    """
    Lightweight audit: checks that MCO portal login pages load and have
    the expected username/password fields. Does NOT attempt login.
    """
    SESSION_NAME = "mco_audit"

    MCO_PAGES = {
        "sentara":  ("https://apps.sentarahealthplans.com/providers/login/login.aspx",
                     ["input[name*='user']", "input[type='password']"]),
        "united":   ("https://www.uhcprovider.com",
                     ["input[name*='user'], input[id*='user']", "input[type='password']"]),
        "availity": ("https://apps.availity.com/",
                     ["input[name*='user'], input[id*='user']", "input[type='password']"]),
        "kepro":    ("https://portal.kepro.com/Home/Index",
                     ["input[name*='user']", "input[type='password']"]),
        "nextiva":  ("http://vfax.nextiva.com",
                     ["input[name*='user']", "input[type='password']"]),
    }

    @property
    def login_url(self) -> str:
        return "https://apps.availity.com/"

    async def _is_logged_in(self) -> bool:
        return False

    async def _perform_login(self) -> bool:
        return False

    async def audit(self) -> AuditReport:
        import asyncio
        report = AuditReport(portal="mco_portals")
        for mco_name, (url, selectors) in self.MCO_PAGES.items():
            try:
                await self.page.goto(url, wait_until="domcontentloaded", timeout=15_000)
                await asyncio.sleep(1)
                for sel in selectors:
                    found = False
                    for part in sel.split(","):
                        el = await self.page.query_selector(part.strip())
                        if el:
                            found = True
                            break
                    report.results.append(SelectorResult(
                        portal=f"mco:{mco_name}",
                        selector_name=sel[:40],
                        selector_value=sel,
                        found=found,
                        page_url=url,
                    ))
            except Exception as e:
                report.results.append(SelectorResult(
                    portal=f"mco:{mco_name}",
                    selector_name="PAGE_LOAD",
                    selector_value=url,
                    found=False,
                    page_url=url,
                    error=str(e),
                ))
        return report


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

async def run_audit(portal: str, headless: bool) -> List[AuditReport]:
    reports = []

    auditors = {
        "claimmd":  lambda: ClaimMDAuditor(headless=headless),
        "lauris":   lambda: LaurisAuditor(headless=headless),
        "mco":      lambda: MCOPortalAuditor(headless=headless),
    }

    targets = list(auditors.keys()) if portal == "all" else [portal]

    for target in targets:
        if target not in auditors:
            print(f"Unknown portal: {target}. Valid: {list(auditors.keys()) + ['all']}")
            continue
        print(f"\nAuditing {target}...")
        try:
            auditor = auditors[target]()
            async with auditor as session:
                report = await session.audit()
            report.print_summary()
            report.save()
            reports.append(report)
        except Exception as e:
            print(f"  ERROR auditing {target}: {e}")

    return reports


if __name__ == "__main__":
    setup_logging()
    parser = argparse.ArgumentParser(description="LCI Portal Selector Auditor")
    parser.add_argument(
        "--portal", default="claimmd",
        choices=["claimmd", "lauris", "mco", "all"],
        help="Which portal to audit",
    )
    parser.add_argument(
        "--headless", default="true",
        choices=["true", "false"],
        help="Run browser headlessly",
    )
    args = parser.parse_args()
    asyncio.run(run_audit(args.portal, args.headless == "true"))
