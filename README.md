# LCI Claims Automation

Full end-to-end automation for Life Consultants Inc. claims follow-up, correction,
MCO portal auth verification, Lauris fax verification, Claim.MD fix/resubmit,
and Lauris root-cause remediation.

## Quick Start

```bash
cp .env.template .env          # Fill in credentials
pip install -r requirements.txt
playwright install chromium

python orchestrator.py --dry-run    # Test without submitting anything
python orchestrator.py              # Run today's scheduled tasks
python orchestrator.py --schedule   # Start cron-style (Mon-Fri 7am)
```

## Single Action Runs

```bash
python orchestrator.py --action era       # ERA upload only
python orchestrator.py --action correct   # Claim corrections only
python orchestrator.py --action recon     # Reconsiderations only
python orchestrator.py --action writeoff  # Write-offs only
python orchestrator.py --action auth      # MCO auth checks only
```

## Weekly Billing (Tuesday)

```bash
python -c "import asyncio; from actions.billing_submission import run_weekly_billing; asyncio.run(run_weekly_billing())"
```

## Tests

```bash
pytest tests/test_all.py -v     # 71 tests
```

## Architecture

```
orchestrator.py              ← Main entry point + scheduler
config/
  settings.py                ← Env + live Google Sheet credential loader
  models.py                  ← All dataclasses and enums
decision_tree/
  router.py                  ← Denial code → ResolutionAction mapping
notes/
  formatter.py               ← Mandatory #INITIALS #MM/DD/YY format enforcer
sources/
  browser_base.py            ← Playwright base (session persistence, MFA)
  claimmd.py                 ← Claim.MD: login, claim list, corrections, recon, appeals
  powerbi.py                 ← Power BI: REST API export + browser scrape fallback
lauris/
  billing.py                 ← ERA upload, write-offs, auth management, fax proxy
mco_portals/
  auth_checker.py            ← Sentara, United, Availity (Molina/Anthem/Aetna), Kepro
actions/
  handlers.py                ← High-level orchestrated workflows per action type
  fax_refax.py               ← Nextiva fax: cover letter builder + send workflow
  billing_submission.py      ← Weekly billing: Double Billing Report + submit
exceptions/
  human_review_queue.py      ← Flags for human review, saves JSON, posts to ClickUp
logging_utils/
  logger.py                  ← Structured JSON logging + ClickUp + Google Sheets
```

## Critical Rules (from Admin Manual)

1. **Never click Save on reconsideration notes** — moves claim to transmit queue
2. **Anthem Mary's Home ERA** — NEVER auto-upload (separate manual process)
3. **United authorizations** — NOT faxed. If missing, create urgent ClickUp task
4. **Aetna billing** — excluded from Tuesday billing run (separate schedule)
5. **Aetna reconsiderations** — require 5 extra documents (Lauris note, assessment, ISP, DMAS regs, auth)
6. **RRR claims** — write off immediately upon ERA receipt, every day
7. **Claims < 7 days old** — do not follow up unless rejected
8. **Note format** — `#` only for initials+date suffix. Never in note body.
9. **Billing** — verify payroll has NOT run before submitting. Run Double Billing Report first.
10. **Magellan** — always phone call first. Never auto-submit.

## MFA Strategy

| Portal   | MFA Type         | Automation Strategy              |
|----------|-----------------|----------------------------------|
| Sentara  | Duo Push        | Auto-click push button, poll     |
| Availity | Varies          | TOTP if supported, else manual   |
| United   | Standard login  | Session cookie reuse             |
| Kepro    | Standard login  | Session cookie reuse             |
| Lauris   | Standard login  | Session cookie reuse             |

For portals with mandatory Duo push where API access isn't available:
set `SENTARA_MFA_TYPE=manual` in .env and the automation will pause
and notify the operator to complete MFA.
