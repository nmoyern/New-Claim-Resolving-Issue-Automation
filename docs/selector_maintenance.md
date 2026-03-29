# Selector Maintenance Guide

Portal UIs change. When the automation starts failing for a specific portal,
follow this guide to find and update the broken selectors.

---

## Step 1 — Run the Selector Auditor

```bash
# Check Claim.MD selectors
python tools/selector_audit.py --portal claimmd --headless false

# Check Lauris
python tools/selector_audit.py --portal lauris --headless false

# Check all MCO portal login pages
python tools/selector_audit.py --portal mco --headless false

# Check everything
python tools/selector_audit.py --portal all --headless false
```

The auditor will:
- Navigate each portal page
- Try every selector used in the automation
- Report which ones fail
- Save screenshots of missing elements to `/tmp/claims_selector_audit/`

---

## Step 2 — Find the New Selector

Open the browser DevTools on the failing page:
1. Right-click the element → Inspect
2. In the Elements panel, right-click the `<input>` or `<button>` → Copy → Copy selector
3. Or use `document.querySelector(...)` in the Console tab to test selectors

**Good selectors (most stable → least stable):**
1. `input[name="specificName"]` — name attributes rarely change
2. `input[id="specificId"]` — IDs are stable if not auto-generated
3. `button:has-text('Exact Text')` — button text sometimes changes
4. `.class-name` — class names change most frequently
5. `nth-child` / positional — avoid these entirely

---

## Step 3 — Update the Selector

Each portal's selectors are in a `SELECTORS` dict at the top of its module:

| Portal | File | Dict name |
|--------|------|-----------|
| Claim.MD | `sources/claimmd.py` | `SELECTORS` |
| Lauris | `lauris/billing.py` | (inline in methods, see `SELECTORS` section) |
| Lauris Fax | `lauris/billing.py` | (in `_navigate_to_fax_proxy` etc.) |
| MCO Portals | `mco_portals/auth_checker.py` | (per-MCO inline selectors) |
| Nextiva Fax | `actions/fax_refax.py` | (in `NextivaFaxSession`) |

For Claim.MD, update the entry in the `SELECTORS` dict at the top of `sources/claimmd.py`:

```python
SELECTORS = {
    # OLD:
    "notes_field": "textarea#notes, textarea[name='notes'], #claim_notes",
    # NEW (after portal update):
    "notes_field": "textarea[data-testid='claim-notes'], #noteInput",
}
```

Comma-separated values are tried in order — put the most reliable first.

---

## Step 4 — Verify

```bash
# Run the auditor again to confirm fix
python tools/selector_audit.py --portal claimmd --headless false

# Run the test suite
python -m pytest tests/ -v

# Dry run against live portals
python orchestrator.py --dry-run --action correct
```

---

## Common Failure Patterns

### Claim.MD

| Symptom | Likely cause | Where to fix |
|---------|-------------|-------------|
| Can't click "Manage Claims" | Left nav restructured | `manage_claims_link` in SELECTORS |
| Denied tab not found | Tab renamed or moved | `denied_tab` in SELECTORS |
| Notes field not found | Notes area redesigned | `notes_field` in SELECTORS |
| "Other Actions" missing | Button moved or renamed | `other_actions` in SELECTORS |
| Appeal form dropdown empty | Form names changed | `MCO_APPEAL_FORM_NAMES` dict in `claimmd.py` |

### Lauris

| Symptom | Likely cause | Where to fix |
|---------|-------------|-------------|
| Billing center unreachable | URL or nav changed | `_navigate_to_billing_center()` in `billing.py` |
| Fax proxy not opening | Applications menu changed | `_navigate_to_fax_proxy()` in `billing.py` |
| Write-off button missing | Billing UI updated | `write_off_claim()` in `billing.py` |
| ERA upload failing | Upload flow changed | `upload_era()` in `billing.py` |

### MCO Portals

| MCO | Common issue | Fix location |
|-----|-------------|-------------|
| Sentara | Duo MFA prompt changed | `_mfa_duo_push()` in `browser_base.py` |
| Sentara | Auth search path changed | `check_auth()` in `SentaraPortal` |
| Availity | Payer Spaces layout changed | `check_auth_molina/anthem/aetna()` in `AvailityPortal` |
| United | TrackIt URL changed | `submit_reconsideration_trackit()` in `UnitedPortal` |
| Kepro | Context selector moved | `check_auth()` in `KoproPortal` |

---

## Phone Number Updates

MCO phone numbers change. They are stored in the Admin Logins Google Sheet.
The automation reads them at runtime via `get_credentials()` — update the
sheet, not the code.

Emergency manual update (if sheet unavailable):
```python
# In config/settings.py or .env
SENTARA_PHONE = "1-855-214-3822"
```

---

## MFA Changes

If a portal adds or changes MFA:
1. Update the `mfa_type` column in the Admin Logins Google Sheet
2. Valid values: `none`, `manual`, `totp`, `duo_push`
3. For TOTP: set `{PORTALNAME}_TOTP_SECRET` in `.env`

---

## Escalation

If a portal has a major redesign (full SPA rewrite, login flow change):
1. Run `python tools/selector_audit.py --portal [name] --headless false` first
2. Open browser DevTools and map out new element structure
3. Update selectors + add a new `SELECTORS` entry if needed
4. Run full test suite before deploying

All changes should be tested with `--dry-run` before going live.
