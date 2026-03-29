#!/usr/bin/env python3
"""
tools/test_portals.py
---------------------
Test login to each MCO portal and verify session.

Usage:
    python tools/test_portals.py sentara    # Test Sentara (needs SMS)
    python tools/test_portals.py united     # Test UHC
    python tools/test_portals.py availity   # Test Availity
    python tools/test_portals.py kepro      # Test Kepro
    python tools/test_portals.py nextiva    # Test Nextiva Fax
    python tools/test_portals.py all        # Test all portals
"""
import asyncio
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_credentials, SESSION_DIR

SESSION_DIR.mkdir(parents=True, exist_ok=True)

PORTALS = {
    "sentara": {
        "name": "Sentara Health Plans",
        "url": "https://apps.sentarahealthplans.com/providers/login/login.aspx",
        "headless": False,  # Needs SMS MFA — must be visible
    },
    "united": {
        "name": "UHC Provider Portal",
        "url": "https://secure.uhcprovider.com/",
        "headless": True,
    },
    "availity": {
        "name": "Availity",
        "url": "https://apps.availity.com/",
        "headless": True,
    },
    "kepro": {
        "name": "Kepro / Atrezzo",
        "url": "https://portal.kepro.com/Home/Index",
        "headless": True,
    },
    "nextiva": {
        "name": "Nextiva Fax",
        "url": "https://fax.nextiva.com/xauth/",
        "headless": True,
    },
    "lauris": {
        "name": "Lauris EMR",
        "url": None,
        "headless": True,
    },
}


async def test_portal(portal_key: str):
    """Test login to a single portal."""
    from playwright.async_api import async_playwright

    if portal_key not in PORTALS:
        print(f"Unknown portal: {portal_key}")
        print(f"Available: {', '.join(PORTALS.keys())}")
        return False

    info = PORTALS[portal_key]
    creds = get_credentials()
    portal_creds = getattr(creds, portal_key, None)

    url = info["url"] or (portal_creds.url if portal_creds else "")
    if not url:
        print(f"  No URL for {portal_key}")
        return False

    session_file = SESSION_DIR / f"{portal_key}_{date.today().isoformat()}.json"
    headless = info["headless"]

    print(f"\n{'='*50}")
    print(f"  Testing: {info['name']}")
    print(f"  URL: {url}")
    print(f"  Headless: {headless}")
    print(f"{'='*50}")

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=headless)

    # Try restoring session
    if session_file.exists():
        context = await browser.new_context(storage_state=str(session_file))
        print(f"  Restored session from {session_file.name}")
    else:
        context = await browser.new_context()

    page = await context.new_page()

    try:
        await page.goto(url, wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(3)

        title = await page.title()
        current_url = page.url
        print(f"  Page: {title}")
        print(f"  URL: {current_url}")

        # Try to fill credentials
        if portal_creds and portal_creds.username:
            for sel in [
                "input[name='username']", "input[name='userlogin']",
                "input[name='LogonUserName']", "input[id*='User']",
                "input[type='text']",
            ]:
                el = await page.query_selector(sel)
                if el:
                    await el.fill(portal_creds.username)
                    print(f"  Username filled: {portal_creds.username}")
                    break

            for sel in [
                "input[type='password']", "input[name='password']",
                "input[name='LogonUserPassword']",
            ]:
                el = await page.query_selector(sel)
                if el:
                    await el.fill(portal_creds.password)
                    print(f"  Password filled: ****")
                    break

        if not headless:
            # For Sentara — user needs to handle MFA manually
            print(f"\n  >> Complete login in the browser, then press ENTER here...")
            try:
                await asyncio.get_event_loop().run_in_executor(None, input)
            except EOFError:
                await asyncio.sleep(60)

        else:
            # Try clicking login button
            for sel in [
                "button[type='submit']", "input[type='submit']",
                "button:has-text('Log In')", "input[value='Log In']",
                "input[value='Logon']", "button:has-text('Sign In')",
            ]:
                btn = await page.query_selector(sel)
                if btn:
                    await btn.click()
                    print(f"  Login button clicked")
                    break
            await asyncio.sleep(4)

        # Save session
        await context.storage_state(path=str(session_file))

        # Check result
        final_title = await page.title()
        final_url = page.url
        print(f"\n  Result: {final_title}")
        print(f"  URL: {final_url}")

        # Take screenshot
        ss_path = f"logs/screenshots/portal_test_{portal_key}.png"
        await page.screenshot(path=ss_path, full_page=True)
        print(f"  Screenshot: {ss_path}")

        # Check for logged-in indicators
        for sel in [
            "a[href*='logout']", "a:has-text('Sign Out')",
            "a:has-text('SIGN OUT')", ".dashboard",
        ]:
            el = await page.query_selector(sel)
            if el:
                print(f"  STATUS: LOGGED IN")
                await context.close()
                await browser.close()
                await pw.stop()
                return True

        if "login" not in final_url.lower():
            print(f"  STATUS: LIKELY LOGGED IN (not on login page)")
            await context.close()
            await browser.close()
            await pw.stop()
            return True

        print(f"  STATUS: LOGIN UNCLEAR — check screenshot")

    except Exception as e:
        print(f"  ERROR: {e}")

    try:
        await context.close()
        await browser.close()
    except Exception:
        pass
    await pw.stop()
    return False


async def main():
    if len(sys.argv) < 2:
        print("Usage: python tools/test_portals.py <portal>")
        print(f"Portals: {', '.join(PORTALS.keys())}, all")
        sys.exit(1)

    target = sys.argv[1].lower()
    results = {}

    if target == "all":
        for key in PORTALS:
            results[key] = await test_portal(key)
    else:
        results[target] = await test_portal(target)

    print(f"\n{'='*50}")
    print("  RESULTS:")
    for portal, success in results.items():
        status = "OK" if success else "FAILED"
        print(f"    {portal}: {status}")
    print(f"{'='*50}")


if __name__ == "__main__":
    asyncio.run(main())
