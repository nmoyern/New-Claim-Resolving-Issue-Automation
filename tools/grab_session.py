#!/usr/bin/env python3
"""
tools/grab_session.py
---------------------
Grabs login session from your REAL Chrome browser.

HOW TO USE:
  1. Quit Chrome completely (Cmd+Q)
  2. Relaunch Chrome with debug mode (paste in Terminal):
       /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug &
  3. Log into the portal normally in that Chrome window
  4. Run: python tools/grab_session.py claimmd

The script connects to your running Chrome, grabs the cookies,
and saves them for the automation to use.
"""
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from config.settings import SESSION_DIR

SESSION_DIR.mkdir(parents=True, exist_ok=True)

PORTALS = {
    "claimmd": ("Claim.MD", "https://www.claim.md/login"),
    "sentara": ("Sentara", "https://apps.sentarahealthplans.com/providers/login/login.aspx"),
    "lauris": ("Lauris EMR", "https://www12.laurisonline.com"),
    "united": ("UHC Provider", "https://secure.uhcprovider.com/"),
    "availity": ("Availity", "https://apps.availity.com/"),
    "kepro": ("Kepro / Atrezzo", "https://portal.kepro.com/Home/Index"),
    "nextiva": ("Nextiva Fax", "https://fax.nextiva.com/xauth/"),
}

CDP_URL = "http://127.0.0.1:9222"


async def grab_from_chrome(portal_key: str):
    """Connect to running Chrome via CDP and grab session cookies."""
    from playwright.async_api import async_playwright

    if portal_key not in PORTALS:
        print(f"Unknown portal: {portal_key}")
        print(f"Available: {', '.join(PORTALS.keys())}")
        return False

    name, base_url = PORTALS[portal_key]
    session_file = SESSION_DIR / f"{portal_key}_{date.today().isoformat()}.json"

    print(f"\n  Connecting to your Chrome browser...")

    pw = await async_playwright().start()

    try:
        browser = await pw.chromium.connect_over_cdp(CDP_URL)
    except Exception as e:
        print(f"\n  Could not connect to Chrome on port 9222.")
        print(f"  Error: {e}")
        print(f"\n  TO FIX THIS:")
        print(f"  1. Quit Chrome completely (Cmd+Q)")
        print(f"  2. Paste this in Terminal to relaunch Chrome with debug mode:")
        print(f'     /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug &')
        print(f"  3. Log into {name} in that Chrome window")
        print(f"  4. Run this command again")
        await pw.stop()
        return False

    contexts = browser.contexts
    if not contexts:
        print(f"  No browser contexts found. Open a tab in Chrome first.")
        await pw.stop()
        return False

    # Use the first (default) context
    context = contexts[0]
    pages = context.pages
    print(f"  Connected! Found {len(pages)} tab(s).")

    # Check if any tab is on the portal
    portal_tab = None
    for p in pages:
        if base_url.split("//")[1].split("/")[0] in p.url:
            portal_tab = p
            print(f"  Found {name} tab: {p.url}")
            break

    if not portal_tab:
        print(f"  No {name} tab found. Make sure you're logged into {name} in Chrome.")
        print(f"  Open tabs: {[p.url for p in pages]}")

    # Save the session state (all cookies from all tabs)
    await context.storage_state(path=str(session_file))

    print(f"\n  Session saved: {session_file}")
    print(f"  The automation will use these cookies today.")

    # Don't close the browser — it's the user's Chrome!
    await pw.stop()
    return True


async def main():
    if len(sys.argv) < 2:
        print("Usage: python tools/grab_session.py <portal>")
        print(f"Portals: {', '.join(PORTALS.keys())}, all")
        print()
        print("SETUP (one time):")
        print("  1. Quit Chrome (Cmd+Q)")
        print("  2. Relaunch with debug mode:")
        print('     /Applications/Google Chrome.app/Contents/MacOS/Google Chrome --remote-debugging-port=9222 --user-data-dir=/tmp/chrome-debug &')
        print("  3. Log into your portals normally")
        print("  4. Run: python tools/grab_session.py claimmd")
        sys.exit(1)

    target = sys.argv[1].lower()
    if target == "all":
        for key in PORTALS:
            await grab_from_chrome(key)
    else:
        await grab_from_chrome(target)


if __name__ == "__main__":
    asyncio.run(main())
