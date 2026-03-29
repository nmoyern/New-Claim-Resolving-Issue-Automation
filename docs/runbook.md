# Go-Live Runbook

Step-by-step checklist for deploying LCI Claims Automation to production.

---

## Pre-Deployment Checklist

### 1. Credentials
- [ ] Fill in all values in `.env` (copy from `.env.template`)
- [ ] Claim.MD username and password verified
- [ ] Lauris URL, username, password verified
- [ ] ClickUp API token added and tested:
  ```bash
  curl -H "Authorization: YOUR_TOKEN" https://api.clickup.com/api/v2/user
  ```
- [ ] Google Service Account JSON created with Sheets + Drive scope
- [ ] Service account shared on Admin Logins Sheet (Editor)
- [ ] Service account shared on Claim Denial Calls Sheet (Editor)
- [ ] `GOOGLE_SERVICE_ACCOUNT_JSON` path set in `.env`

### 2. MCO Portals
- [ ] Sentara login verified, MFA type set in Admin Logins Sheet
- [ ] United / UHC login verified
- [ ] Availity login verified (covers Molina, Anthem, Aetna)
- [ ] Kepro/Atrezzo login verified
- [ ] Nextiva Fax login verified
- [ ] All MCO fax numbers populated in Admin Logins Sheet

### 3. Installation
```bash
chmod +x install.sh
./install.sh --dev
```

### 4. Selector Audit
```bash
# Run BEFORE any live automation
python tools/selector_audit.py --portal claimmd --headless false
python tools/selector_audit.py --portal mco --headless false
```
- [ ] No critical failures in audit
- [ ] Screenshots reviewed for any issues

### 5. Dry Run
```bash
python orchestrator.py --dry-run
```
- [ ] No import errors
- [ ] ClickUp comment posted to task 86ad83kf7
- [ ] Google Sheets row appended to Claim Denial Calls sheet
- [ ] Log file created in `/var/log/claims_automation/`
- [ ] Human review queue file created (even if empty)

### 6. Live Test (Single Action)
```bash
# Test ERA download + upload only
python orchestrator.py --action era --max-claims 0

# Test write-offs only on 5 RRR claims
python orchestrator.py --action writeoff --max-claims 5
```
- [ ] ERAs downloaded from Claim.MD
- [ ] ERAs uploaded to Lauris (non-irregular only)
- [ ] Irregular ERAs flagged in ClickUp comment

### 7. Full Live Run (Limited)
```bash
python orchestrator.py --max-claims 20
```
- [ ] Claims fetched from Claim.MD
- [ ] Routing decisions logged
- [ ] At least one correction submitted
- [ ] At least one reconsideration submitted
- [ ] ClickUp daily comment reflects actual numbers
- [ ] Human review queue saved with correct items

---

## Scheduling

### Linux/Mac (cron)
```bash
# Run Mon-Fri at 7:00 AM
crontab -e
# Add:
0 7 * * 1-5 cd /path/to/claims_automation && python orchestrator.py >> /var/log/claims_automation/cron.log 2>&1
```

### Windows (Task Scheduler)
```cmd
schtasks /create /xml "LCI_Claims_Daily.xml" /tn "LCI Claims Automation"
```
Edit `LCI_Claims_Daily.xml` first — update `<Command>` and `<WorkingDirectory>` paths.

### Background Process (APScheduler)
```bash
# Runs as long as process is alive
python orchestrator.py --schedule
# Use screen/tmux/systemd to keep it alive
screen -S claims_automation
python orchestrator.py --schedule
# Ctrl+A, D to detach
```

---

## Monitoring

### Daily
- Check ClickUp task `86ad83kf7` — automation should post a comment by 9 AM
- Review human review queue: `/var/log/claims_automation/human_review_YYYY-MM-DD.json`
- Spot-check 3-5 Claim.MD notes to verify correct format (`#INITIALS #MM/DD/YY`)

### Weekly
- Compare Power BI "Total Outstanding AR" before and after the week
- Review write-off totals (should mirror what Desiree sees in weekly KPI)
- Check for recurring human review items — these may need new automation rules

### If Something Breaks
1. Check log file: `/var/log/claims_automation/claims_YYYY-MM-DD.jsonl`
2. Check screenshots: `/var/log/claims_automation/screenshots/`
3. Run selector audit: `python tools/selector_audit.py --portal claimmd`
4. Run with `--dry-run` to isolate
5. See `docs/selector_maintenance.md` for portal-specific fixes

---

## Rollback

The automation **only adds** — it does not delete or modify existing Claim.MD
records except through:
- Claim corrections (which retransmit and add notes)
- Reconsideration submissions (adds notes, no save = not in transmit queue)
- Write-offs (marks claims in Lauris)

To pause the automation:
```bash
# Stop the scheduler
kill $(pgrep -f orchestrator.py)
# Or disable the cron job / Task Scheduler task
```

No data is permanently deleted. All actions are logged and reversible by
re-entering the portals manually.

---

## Contact

- **Technical issues**: Vitor Costa (systems/automation)
- **Billing process questions**: Desiree Whitehead
- **Portal access issues**: Admin Logins Google Sheet → check with Desiree
- **MCO portal MFA problems**: Call the MCO provider services line
