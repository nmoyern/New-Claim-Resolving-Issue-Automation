# New Claim Resolving Issue Automation

This repo is the smaller, safer version of the old claims automation.

It only works on claims that meet both rules:

1. The claim was already billed.
2. Claim.MD says the claim came back rejected or denied.

It does not try to run billing, search fax systems, refax paperwork, or chase
old portal/document workflows.

## Payer Lookup Rules

Before the automation works a claim, it asks the payer what the payer currently
sees.

| Claim payer | API used |
|-------------|----------|
| United Healthcare / UHC | Optum Real Claim Inquiry |
| All other MCOs | Availity claim status |

Plain-English buckets:

| Bucket | Meaning | What the automation does |
|--------|---------|--------------------------|
| `paid_at_payer` | The payer says it paid the claim. | Skip claim work. |
| `too_new` | The payer has it, but has not decided yet. | Skip for now. |
| `real_denial` | The payer confirms it denied the claim. | Route for resolution. |
| `payer_rejected` | The payer rejected the claim at intake. | Route for correction. |
| `payer_no_record` | The payer cannot find the claim. | Route for follow-up. |
| `api_error` | The API could not answer. | Keep in the work queue. |

## What It Still Uses

- Claim.MD: pulls the rejected/denied claims and applies supported corrections.
- ERA workflow: downloads, stages, and posts ERAs so paid claims can clear.
- Optum: checks United Healthcare claim status.
- Availity: checks claim status for the other MCOs.
- ClickUp / Sheets logging: records what happened.
- ClickUp follow-up: checks completed tasks and comments so human answers can
  move claims to the next step.
- Lauris DOB/gender view: enriches claims with demographics needed for payer
  API matching.

## What It Does Not Use In The Main Run

- Fax systems
- Nextiva refax workflows
- Lauris fax verification
- Dropbox auth confirmation searches
- Pre-billing checks
- Billing submission

## Quick Start

```bash
touch .env
pip install -r requirements.txt

python orchestrator.py --dry-run
python orchestrator.py
python orchestrator.py --schedule
```

## Classification Dry Run

Use this before trusting any live claim-changing work:

```bash
python tools/classification_dry_run.py --max-claims 25
```

This creates two files under Dropbox > Chesapeake LCI > AR Reports >
Claim Resolution > Classification Dry Runs:

- a readable Markdown report for business review
- a JSON report for technical review

Each filename includes the date, time, and microseconds so running the report
multiple times in the same day will not overwrite an older report.

If the local Dropbox folder is not available, the report writer saves a local
fallback copy and uploads it to Dropbox through the Dropbox API when these are
configured:

```bash
DROPBOX_REFRESH_TOKEN=...
DROPBOX_APP_KEY=...
DROPBOX_APP_SECRET=...
```

`DROPBOX_ACCESS_TOKEN` also works for short-lived/manual runs.

Plain-English purpose:

1. Pull rejected/denied Claim.MD claims without moving the Claim.MD cursor.
2. Keep only claims that were actually billed.
3. Record the old claim context: denial reason, billing company, NPI, auth,
   Lauris Unique ID, prior follow-up, notes, and dates.
4. Check payer status through Optum or Availity when credentials are available.
5. Run the company/auth match decision tree.
6. Show what the system would do next.

No-authorization rule:

- If Claim.MD denied the claim for no authorization and the claim has no auth
  number, the system must obtain or confirm the authorization before
  resubmitting.
- ClickUp requests are grouped by Lauris Unique ID so one person does not get
  several separate tasks for multiple claim lines.
- Each request lists the Claim ID, DOS, MCO, CPT code, service code, program,
  and billed amount.

It does not post ERAs, update Claim.MD, create ClickUp tasks, or change any
claim data.

## Required API Settings

Claim.MD:

```bash
CLAIMMD_API_KEY=...
```

Optum for United Healthcare:

```bash
OPTUM_CLIENT_ID=...
OPTUM_CLIENT_SECRET=...
OPTUM_PROVIDER_TAX_ID=...
OPTUM_TOKEN_URL=https://sandbox-apigw.optum.com/apip/auth/sntl/v1/token
OPTUM_BASE_URL=https://sandbox-apigw.optum.com/oihub/claim/inquiry/v1
OPTUM_ENVIRONMENT=sandbox
```

Availity for all other MCOs:

```bash
AVAILITY_PROD_CLIENT_ID=...
AVAILITY_PROD_CLIENT_SECRET=...
AVAILITY_BASE_URL=https://api.availity.com
```

## Billing Entities

The source of truth lives in `config/entities.py`.

| Entity | Program | Billing NPI | Tax ID / EIN |
|--------|---------|-------------|--------------|
| Mary's Home | `MARYS_HOME` | `1437871753` | `861567663` |
| NHCS | `NHCS` | `1700297447` | `465232420` |
| KJLN | `KJLN` | `1306491592` | `821966562` |

## Company/Auth Match Rule

The classifier lives in `actions/company_auth_match.py`.

Plain-English rule:

1. Check whether the authorization matches the claim's current company.
2. If not, check Mary's Home, NHCS, and KJLN.
3. If exactly one different company matches, prepare these changes:
   - billing company / region
   - billing NPI
   - Tax ID / EIN
   - auth number, if found
4. If zero or multiple companies match, send to human review.

The classifier only recommends changes right now. It does not modify Claim.MD
until the auto-update step is wired in.

The payer lookup adapter lives in `sources/payer_auth_lookup.py`:

- United / UHC uses Optum auth/referral lookup.
- Other MCOs use the Availity entity-sweep signal.
- If an API lacks enough data, it returns no match instead of guessing.

DOB/gender enrichment lives in `sources/lauris_demographics.py`. It pulls the
Lauris `Claim_DOB__x0026__Gender_AUTOMATION` XML view and attaches `client_dob`
and `gender_code` to claims before payer lookups.

## Single Action Runs

```bash
python orchestrator.py --action correct
python orchestrator.py --action era
python orchestrator.py --action recon
python orchestrator.py --action writeoff
python orchestrator.py --action auth
```

## Main Files

```text
orchestrator.py              Main daily run
sources/payer_inquiry.py     Optum vs Availity API routing
sources/claimmd_api.py       Claim.MD rejected/denied claim pull
decision_tree/router.py      What to do with each confirmed problem claim
actions/handlers.py          Action handlers for corrections/reconsiderations/etc.
```
