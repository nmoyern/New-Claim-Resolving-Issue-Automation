"""
tools/test_bank_portals.py
---------------------------
Test bank portal logins one at a time (headful — visible browser).

Usage:
    cd claims_automation
    python -m tools.test_bank_portals              # test all 3
    python -m tools.test_bank_portals wellsfargo    # test one
    python -m tools.test_bank_portals southernbank
    python -m tools.test_bank_portals bankofamerica
"""
from __future__ import annotations

import asyncio
import sys
import os

# Ensure project root is on path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv()

# Force DRY_RUN off so browser actually launches
os.environ["DRY_RUN"] = "false"

from reconciliation.bank_portals import (
    WellsFargoPortal,
    SouthernBankPortal,
    BankOfAmericaPortal,
)

PORTALS = {
    "wellsfargo": WellsFargoPortal,
    "southernbank": SouthernBankPortal,
    "bankofamerica": BankOfAmericaPortal,
}


async def test_portal(name: str, cls):
    """Test a single bank portal login."""
    print(f"\n{'='*60}")
    print(f"  TESTING: {name}")
    print(f"{'='*60}")

    portal = cls(headless=False)  # Visible browser

    print(f"  Login URL: {portal.login_url}")
    print(f"  Session: {portal._session_file}")

    try:
        async with portal:
            if portal.page is None:
                print(f"  [SKIP] DRY_RUN is enabled")
                return

            print(f"  Current URL: {portal.page.url}")
            logged_in = await portal._is_logged_in()
            print(f"  Logged in: {logged_in}")

            if logged_in:
                print(f"  [OK] Login successful!")

                # Take a screenshot of the dashboard
                await portal.screenshot(f"test_{name}_dashboard")
                print(f"  Screenshot saved.")

                # Try to get recent deposits
                print(f"  Fetching recent deposits...")
                try:
                    deposits = await portal.get_recent_deposits(days=7)
                    print(f"  Found {len(deposits)} deposits:")
                    for dep in deposits[:10]:
                        print(
                            f"    {dep.get('date', '?'):12s} "
                            f"${dep.get('amount', 0):>10,.2f}  "
                            f"{dep.get('description', '')[:60]}"
                        )
                    if not deposits:
                        print("    (no deposits found — may need to adjust "
                              "navigation selectors)")
                except Exception as e:
                    print(f"  [WARN] Deposit fetch failed: {e}")
                    print("  This is expected on first run — we need to see "
                          "the actual page structure.")
                    await portal.screenshot(f"test_{name}_deposits_page")
                    print(f"  Screenshot saved for selector inspection.")
            else:
                print(f"  [FAIL] Login failed or MFA timed out.")
                print(f"  Current URL: {portal.page.url}")
                await portal.screenshot(f"test_{name}_login_failed")
                print(f"  Screenshot saved for debugging.")

            # Keep browser open for 10 seconds so you can inspect
            print(f"\n  Browser stays open 10s for inspection...")
            await asyncio.sleep(10)

    except Exception as e:
        print(f"  [ERROR] {e}")
        import traceback
        traceback.print_exc()

    print(f"  Done with {name}.\n")


async def main():
    targets = sys.argv[1:] if len(sys.argv) > 1 else list(PORTALS.keys())

    print("\nBank Portal Login Test")
    print("=" * 60)
    print(f"Testing: {', '.join(targets)}")
    print(f"DRY_RUN: {os.getenv('DRY_RUN', 'false')}")
    print()

    for name in targets:
        name = name.lower().strip()
        if name not in PORTALS:
            print(f"Unknown portal: {name}")
            print(f"Available: {', '.join(PORTALS.keys())}")
            continue
        await test_portal(name, PORTALS[name])

    print("\n" + "=" * 60)
    print("All tests complete.")
    print("Check logs/screenshots/ for debug images.")
    print("=" * 60)


if __name__ == "__main__":
    asyncio.run(main())
