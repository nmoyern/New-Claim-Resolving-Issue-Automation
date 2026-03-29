"""
tools/test_era_posting.py
--------------------------
Test ERA posting page in Lauris — inspects the real EDI Results page.

Usage:
    cd claims_automation
    python3 -m tools.test_era_posting
"""
from __future__ import annotations

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

os.environ["DRY_RUN"] = "false"


async def main():
    from lauris.billing import LaurisSession

    print("\n" + "=" * 60)
    print("  ERA POSTING PAGE INSPECTION")
    print("=" * 60)

    edi_path = "ar/ClosedBillingEDIResults.aspx"

    async with LaurisSession(headless=False) as lauris:
        if lauris.page is None:
            print("  [SKIP] Browser not launched")
            return

        print(f"  Logged into Lauris: {lauris.page.url}")
        await lauris.screenshot("test_lauris_dashboard")

        # Navigate to EDI Results page
        base = lauris.login_url.rsplit("/", 1)[0]
        edi_url = f"{base}/{edi_path}"
        print(f"  Navigating to: {edi_url}")

        await lauris.page.goto(edi_url, wait_until="domcontentloaded",
                               timeout=20000)
        await asyncio.sleep(3)

        print(f"  Current URL: {lauris.page.url}")
        await lauris.screenshot("test_edi_results_page")

        # Inspect the dropdown
        print("\n  --- EDI Files Dropdown ---")
        options = await lauris.page.query_selector_all(
            "select[name='ddlEDIFiles'] option"
        )
        if not options:
            # Try alternate selectors
            for sel in [
                "select option", "select[id*='EDI'] option",
                "select[id*='edi'] option", "select[id*='ddl'] option",
            ]:
                options = await lauris.page.query_selector_all(sel)
                if options:
                    print(f"  Found dropdown with selector: {sel}")
                    break

        if options:
            print(f"  Found {len(options)} options in dropdown:")
            for i, opt in enumerate(options[:20]):
                val = await opt.get_attribute("value") or ""
                text = (await opt.inner_text()).strip()
                print(f"    [{i}] value='{val}' text='{text[:80]}'")
            if len(options) > 20:
                print(f"    ... and {len(options) - 20} more")
        else:
            print("  [WARN] No dropdown options found!")
            print("  Dumping all selects on page:")
            selects = await lauris.page.query_selector_all("select")
            for sel in selects:
                name = await sel.get_attribute("name") or ""
                sel_id = await sel.get_attribute("id") or ""
                print(f"    select name='{name}' id='{sel_id}'")

        # Inspect the Post button
        print("\n  --- Post Button ---")
        for sel in [
            "input[name='btnPostFile']",
            "input[value*='Post']",
            "button:has-text('Post')",
            "input[type='submit']",
        ]:
            btn = await lauris.page.query_selector(sel)
            if btn:
                name = await btn.get_attribute("name") or ""
                val = await btn.get_attribute("value") or ""
                print(f"  Found: selector='{sel}' name='{name}' value='{val}'")
                break
        else:
            print("  [WARN] Post button not found!")
            print("  Dumping all buttons/inputs[submit]:")
            buttons = await lauris.page.query_selector_all(
                "input[type='submit'], input[type='button'], button"
            )
            for btn in buttons:
                name = await btn.get_attribute("name") or ""
                val = await btn.get_attribute("value") or ""
                text = ""
                try:
                    text = (await btn.inner_text()).strip()
                except Exception:
                    pass
                print(f"    name='{name}' value='{val}' text='{text}'")

        # Check for any posted files table/grid
        print("\n  --- Posted Files (if visible) ---")
        tables = await lauris.page.query_selector_all("table")
        print(f"  Found {len(tables)} tables on page")
        for i, table in enumerate(tables[:5]):
            rows = await table.query_selector_all("tr")
            if rows:
                first_row_text = (await rows[0].inner_text()).strip()[:100]
                print(f"    Table {i}: {len(rows)} rows, "
                      f"first row: '{first_row_text}'")

        # Check for error/success message areas
        print("\n  --- Message Areas ---")
        for sel in [
            ".alert", ".message", ".error", ".success",
            "#lblMessage", "#lblError", "span[id*='lbl']",
        ]:
            el = await lauris.page.query_selector(sel)
            if el:
                text = (await el.inner_text()).strip()
                if text:
                    print(f"  {sel}: '{text[:100]}'")

        # DRY RUN: Select first real file but DON'T post
        if options and len(options) > 1:
            # Select the second option (first is usually "Choose...")
            test_val = await options[1].get_attribute("value") or ""
            test_text = (await options[1].inner_text()).strip()
            print(f"\n  --- DRY RUN: Would select ---")
            print(f"  File: '{test_text}'")
            print(f"  Value: '{test_val}'")
            print("  (NOT clicking Post — this is inspection only)")

        print(f"\n  Browser stays open 15s for manual inspection...")
        await asyncio.sleep(15)

    print("\n" + "=" * 60)
    print("  Test complete. Check logs/screenshots/ for images.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
