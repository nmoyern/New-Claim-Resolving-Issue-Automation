#!/usr/bin/env python3
"""
tools/morning_startup.py
-------------------------
Single-command morning startup that handles ALL portal sessions,
then runs the full daily automation.

The ONLY human intervention required: entering SMS auth codes
when they arrive on your phone (276-806-4418).

Usage:
    python tools/morning_startup.py

Flow:
    1. Opens a browser window with ALL portal login tabs
    2. Pre-fills credentials on each
    3. You complete MFA (SMS codes) on Sentara and Availity
    4. You click through login on United and Kepro
    5. Press Enter when all portals are logged in
    6. Sessions are saved automatically
    7. Full daily automation runs immediately
"""
import asyncio
import json
import subprocess
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import get_credentials, SESSION_DIR

SESSION_DIR.mkdir(parents=True, exist_ok=True)

# Portals that need daily session refresh (MFA or bot-blocked)
MFA_PORTALS = [
    # Claim.MD removed — the API handles everything (no browser login needed)
    {
        "key": "sentara",
        "name": "Sentara",
        "url": "https://apps.sentarahealthplans.com/providers/login/login.aspx",
        "note": "Select TEXT to 276-806-4418, enter code",
    },
    {
        "key": "availity",
        "name": "Availity",
        "url": "https://apps.availity.com/",
        "note": "Select TEXT to 276-806-4418, enter code",
    },
    {
        "key": "united",
        "name": "UHC Provider",
        "url": "https://secure.uhcprovider.com/",
        "note": "Sign in normally",
    },
    {
        "key": "kepro",
        "name": "Kepro / Atrezzo",
        "url": "https://portal.kepro.com/Home/Index",
        "note": "Microsoft login — enter email then password",
    },
]


async def run_morning_startup():
    from playwright.async_api import async_playwright

    creds = get_credentials()

    print("\n" + "=" * 60)
    print("  LCI Claims Automation — Morning Startup")
    print("=" * 60)
    print()
    print("  A browser will open with tabs for each portal.")
    print("  Log into each one (you'll need your SMS codes).")
    print("  When ALL portals are logged in, come back here")
    print("  and press ENTER.")
    print()
    print("  Portals to log into:")
    for p in MFA_PORTALS:
        print(f"    • {p['name']}: {p['note']}")
    print()

    pw = await async_playwright().start()
    browser = await pw.chromium.launch(
        headless=False,
        args=["--disable-blink-features=AutomationControlled"],
    )
    context = await browser.new_context(
        user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/122.0.0.0 Safari/537.36"
        )
    )

    # Open each portal in a separate tab
    pages = {}
    for i, portal in enumerate(MFA_PORTALS):
        if i == 0:
            page = await context.new_page()
        else:
            page = await context.new_page()

        await page.goto(portal["url"], wait_until="domcontentloaded", timeout=20000)
        await asyncio.sleep(2)

        # Pre-fill credentials
        portal_creds = getattr(creds, portal["key"], None)
        if portal_creds and portal_creds.username:
            # Try common username selectors
            for sel in [
                "input[name='username']", "input[name='userlogin']",
                "input[name='LogonUserName']", "input[id*='User']",
                "input[name='loginfmt']",  # Microsoft
                "input[type='email']",
            ]:
                el = await page.query_selector(sel)
                if el:
                    await el.fill(portal_creds.username)
                    break

            # Try password
            for sel in [
                "input[type='password']", "input[name='password']",
                "input[name='LogonUserPassword']",
                "input[name='passwd']",  # Microsoft
            ]:
                el = await page.query_selector(sel)
                if el:
                    await el.fill(portal_creds.password)
                    break

        pages[portal["key"]] = page
        print(f"  ✓ Opened {portal['name']}")

    print()
    print("  >> Log into each tab now.")
    print("     SMS codes will go to 276-806-4418.")
    print()
    print("     Press ENTER here when ALL portals are logged in... ",
          end="", flush=True)

    # Wait for user
    try:
        await asyncio.get_event_loop().run_in_executor(None, input)
    except EOFError:
        await asyncio.sleep(120)

    # Save all sessions
    print("\n  Saving sessions...")
    saved = 0
    for portal in MFA_PORTALS:
        key = portal["key"]
        session_file = SESSION_DIR / f"{key}_{date.today().isoformat()}.json"
        try:
            page = pages[key]
            # Get cookies from this specific page's domain
            cookies = await context.cookies(page.url)
            storage = await context.storage_state()
            with open(str(session_file), "w") as f:
                json.dump(storage, f)
            saved += 1
            title = await page.title()
            print(f"  ✓ {portal['name']}: saved ({title})")
        except Exception as e:
            print(f"  ✗ {portal['name']}: failed ({e})")

    print(f"\n  {saved}/{len(MFA_PORTALS)} sessions saved.")

    try:
        await context.close()
        await browser.close()
    except Exception:
        pass
    await pw.stop()

    if saved == 0:
        print("\n  No sessions saved — skipping automation run.")
        return

    # Now run the daily automation
    print("\n" + "=" * 60)
    print("  Running daily claims automation...")
    print("=" * 60 + "\n")

    project_dir = str(Path(__file__).resolve().parent.parent)
    result = subprocess.run(
        [sys.executable, "orchestrator.py", "--action", "all"],
        cwd=project_dir,
        env={**__import__("os").environ, "DRY_RUN": "false"},
    )

    if result.returncode == 0:
        print("\n  ✓ Daily automation complete!")
    else:
        print(f"\n  ✗ Automation exited with code {result.returncode}")


if __name__ == "__main__":
    asyncio.run(run_morning_startup())
