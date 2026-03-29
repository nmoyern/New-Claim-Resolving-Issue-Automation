# LCI Claims Automation -- Operational Walkthrough

**Written for new team members joining the claims follow-up operation.**
**Last updated: March 2026**

---

## 1. What We Do (Big Picture)

We run an automated system that follows up on unpaid healthcare claims for three entities:

- **KJLN Inc.** (NPI: 1306491592)
- **Mary's Home Inc.** (NPI: 1437871753)
- **New Heights Community Support, LLC** aka NHCS (NPI: 1700297447)

**Critical rule: We NEVER use "Life Consultants Inc." in any communications, claim submissions, cover letters, or portal interactions. Always use the specific entity name -- KJLN, Mary's Home, or NHCS.** The function `_get_entity_name()` in `actions/fax_refax.py` enforces this by mapping NPI or program to the correct entity name.

These entities provide mental health and community support services under Virginia Medicaid HCBS waivers. Our clients (consumers) are enrolled in managed care organizations (MCOs) -- Sentara/Optima, Aetna, Molina, United, Anthem, Humana, Magellan, and sometimes straight DMAS Medicaid. When we bill for services and a claim gets denied or rejected, this system figures out why, fixes the problem when possible, and resubmits or escalates.

### How It Runs

The system is triggered two ways:

1. **Scheduled**: Monday through Friday at 7:00 AM via APScheduler (`orchestrator.py`, `start_scheduler()` function, CronTrigger with `day_of_week="mon-fri", hour=7, minute=0`)
2. **Manual**: `bash ~/claims.sh start` -- this runs `python orchestrator.py` with all actions enabled

You can also run specific pieces:
- `python orchestrator.py --action era` -- only ERA upload
- `python orchestrator.py --action correct` -- only corrections
- `python orchestrator.py --dry-run` -- simulates everything without submitting changes
- `python orchestrator.py --full-pull` -- pulls ALL claims from Claim.MD instead of just new ones since last run

The system processes everything outstanding in a single run -- there is no day-of-week scheduling for specific action types anymore (removed March 2026). The function `get_todays_primary_actions()` in `decision_tree/router.py` returns all action types every time.

---

## 2. Daily Workflow -- Step by Step

Here is exactly what happens when the system runs, following the `run_daily()` function in `orchestrator.py`.

### Step 0: Self-Learning Counter and Power BI AR Report

**Self-learning counter** (`reporting/self_learning.py`): The run counter in `data/run_counter.txt` is incremented. Every 10th run, a Self-Learning Report is generated and emailed to `ss@lifeconsultantsinc.org` and `nm@lifeconsultantsinc.org`. This report analyzes a year of data, identifies patterns, and proposes changes. More on this in Section 9.

**Power BI AR Report download** (`sources/powerbi.py`): The system logs into Power BI via Microsoft SSO (email: `nm@lifeconsultantsinc.org`), navigates to the AR report (workspace `8d724e00-8c1d-4d3c-b804-86c163a258c5`, report `39dcf41c-1d1b-428a-9086-a8c7e1f3c0f8`), filters to "Due" and "Under Payment" AR statuses, and exports the middle table as an Excel file.

The `download_powerbi_ar_report()` function:
- Navigates to the report URL
- Waits 20 seconds for the report to fully load (Power BI is slow)
- Sets date range to 1 year lookback
- Selects "Due" and "Under Payment" slicer filters
- Clicks into the middle table area
- Exports via the three-dots menu to Excel
- Saves to `data/powerbi_ar_report.xlsx`

The `load_ar_claims()` function reads the Excel file, mapping columns like Consumer Name, Unique ID, Service Name, Document Date, Billing Amount, Total Outstanding, Member Number, MCO, etc. (see `AR_COLUMN_MAP` in `powerbi.py`).

The `get_ar_work_queue()` function filters to claims where Total Outstanding > 0, sorted by amount descending.

**What this data is for**: Later in Step 2b, we cross-reference Claim.MD denials against this AR data. If a denied claim does NOT appear in the AR (meaning it was already resolved/paid), we skip it. This prevents us from wasting time on claims that were fixed between the denial and today.

**What can go wrong**: Power BI uses position-based mouse clicks as a fallback (coordinates like `(290, 141)` for date inputs, `(277, 470)` for slicer items, `(500, 340)` for the middle table). If the Power BI layout changes, these coordinates will miss. The system tries CSS selectors first, but Power BI's DOM is notoriously inconsistent. If the download fails, the system logs a warning and processes ALL Claim.MD denials without filtering -- which means more work but no data loss.

### Step 1a: ERA Download from Claim.MD

**File**: `actions/era_manager.py`, function `download_and_stage_eras()`

ERAs (Electronic Remittance Advices) are payment notifications from MCOs. They tell us what was paid, what was denied, and why. The system downloads these from Claim.MD's API using the `eralist` and `era835` endpoints.

Each ERA is an 835 file (ANSI X12 format). The system:
1. Calls `api.get_era_list(new_only=True)` to get unprocessed ERAs
2. Checks `data/era_status.json` to skip already-processed ones
3. Downloads each 835 file via `api.download_era_835()`
4. Classifies each ERA using `classify_era()` from `lauris/billing.py`
5. **Standard ERAs** are staged in Dropbox at `~/Library/CloudStorage/Dropbox-LifeConsultantsInc/Chesapeake LCI/AR Reports/ERA Files/Pending Upload/{date}/`
6. **Irregular ERAs** are flagged and skipped

Irregular ERA types that are NEVER auto-processed (defined in `router.py` and `era_poster.py`):
- `anthem_marys` -- Anthem payments for Mary's Home
- `united_marys` -- United payments for Mary's Home
- `recoupment` -- Money being taken back by an MCO
- `straight_medicaid_marys` -- Straight Medicaid for Mary's Home

These require manual handling because they have special processing rules.

A ClickUp notification is posted when new ERAs need manual upload to the Lauris desktop billing center (because the Lauris Billing Center is a Windows desktop app -- ERA upload cannot be fully automated via the web portal).

### Step 1b: ERA Posting to Lauris

**File**: `actions/era_poster.py`, function `post_pending_eras()`

After downloading, the system posts standard ERAs to Lauris via the web-based EDI Results page at `/ar/ClosedBillingEDIResults.aspx`. This page has a dropdown of unposted EDI files.

The process:
1. Navigate to the EDI Results page
2. Get all options from the `ddlEDIFiles` dropdown
3. Load locally tracked posted ERAs from `data/posted_eras.json`
4. For each unposted file:
   - Check against irregular patterns (regex matching on Anthem+Mary's, United+Mary's, recoupment, straight Medicaid+Mary's)
   - Check age (skip files older than 1 year)
   - Select the file from dropdown
   - Click "Post Selected File" (`btnPostFile`)
   - Wait 3 seconds and check for error indicators on the page
   - Take screenshot on errors for debugging
5. Save updated posted ERA tracking
6. Post ClickUp summary

**What can go wrong**: The Lauris session can expire. Error detection is text-based (looking for "error", "failed", "unable to post", "exception", "could not process" in the page text after posting). If posting fails, a screenshot is saved to `/tmp/claims_work/era_post_error_{file_val}.png`.

### Step 1c: Pre-Billing Checks (7 Checks)

**File**: `actions/pre_billing_check.py`, function `run_pre_billing_checks()`

Before any claims are submitted to Claim.MD for the first time, the system runs 7 checks to catch issues that would cause denials. These are run against `pending_claims` from `billing_web.get_pending_claims()`.

See Section 6 for the full breakdown of all 7 checks.

Claims that fail checks are either:
- **Auto-fixed** and proceed to billing
- **Blocked** from submission, with a consolidated ClickUp task created per patient

All issues are logged to the `pre_billing_log` SQLite table in `data/claims_history.db`.

### Step 1d: Billing Submission

**File**: `actions/billing_web.py`, function `run_billing_submission()`

Billing runs whenever the automation is called -- the Wednesday-only restriction was removed. The system:

1. Logs into Lauris
2. Navigates to Close Billing page (`closebillingnc.aspx`)
3. For each billing region (KJLN, NHCS, Mary's Home):
   - Selects the region from `ddlRegion` dropdown
   - Selects the billing date
   - Clicks Refresh (`btnRefresh`) to load billing items
   - **For NHCS**: runs `_adjust_nhcs_mhss_rates()` to ensure MHSS claims are billed at $102.72/unit. This scans the billing grid for rows with MHSS procedure codes (H0046, H2014) and adjusts the charge field
   - Clicks "Close Reconciled Billing Items"
4. Posts results to ClickUp

All MCOs are billed -- the Aetna exclusion was removed. `BILLING_EXCLUDED_MCOS` is an empty set.

### Step 2: Denied Claim Retrieval from Claim.MD

**File**: `sources/claimmd_api.py`, class `ClaimMDAPI`

The system fetches denied and rejected claims from Claim.MD's REST API at `https://svc.claim.md/services/`.

The `get_denied_claims()` function:
1. Loads the last response ID from `data/last_responseid.txt` for incremental fetching (or uses "0" for a full pull)
2. Calls the `response` endpoint to get claim status updates
3. Filters to status codes "R" (rejected), "D" (denied), "4", or "22"
4. Converts raw API responses to `Claim` objects via `_raw_to_claim()`
5. Filters out claims older than 1 year
6. **$0 claims** are archived and queued for a weekly ClickUp task to Justin
7. **Duplicate claims** (same member + DOS + program, different claim IDs) are detected and queued for Justin
8. Saves the new last response ID for the next run

**Payer ID mapping** (in `PAYER_MCO_MAP`): Sentara has IDs 54154, VAPRM, 00453. Aetna is 128VA. Molina is MCCVA. United is 87726, 77350. Anthem is 00423, 00923, SB923. DMAS is SPAYORCODE. Humana is 31140, 61101. Magellan is 38217.

**Program inference from NPI**: NPI 1437871753 = Mary's Home, 1700297447 = NHCS, 1306491592 = KJLN.

**Procedure code to service mapping** (`PROC_SERVICE_MAP`): H0046/H2014 = MHSS, H0019/H2015/H2016 = RCSU, H2015HQ = Community Living, H0036/H2011 = Crisis, H2017/H2018 = PSR.

### Step 2b: Cross-Reference Against Power BI

Back in `orchestrator.py`, if we have AR data from Step 0, we cross-reference each Claim.MD denial:

- **Rejected claims** (status "R") always get processed -- rejections mean the claim never reached the payer, so they need immediate correction
- **Denied claims**: check `is_claim_in_ar(client_id, dos, ar_claims)` -- if the claim is in the AR (still outstanding), process it. If NOT in AR, skip it because it was likely already resolved

This step logs how many claims were filtered out. If AR data was unavailable, ALL Claim.MD denials are processed (more work, but safe).

### Step 3: Claim Routing via Decision Tree

The system separates Rural Rate Reduction (RRR) claims from everything else.

**RRR claims** are processed first -- they go straight to `handle_write_off()` (but see the decision tree in Section 3 for the conditional logic).

**All other claims** go through `router.route(claim)` which returns an `(action, reason)` tuple. The action is checked against `todays_actions` (which is all actions). Then `dispatch(claim, action)` calls the appropriate handler function.

Between claims, there's a 1.5-second delay (`asyncio.sleep(1.5)`) to avoid hammering portals.

Results are tallied into the `DailyRunSummary` and logged to both Google Sheets and the gap report database.

### Step 3a: Check If Corrected Claims Got Paid

**File**: `reporting/autonomous_tracker.py`, function `check_resolved_corrections()`

The system queries `autonomous_corrections` table for unresolved entries and checks Claim.MD API to see if the claim status changed to "A" (Accepted), "1", or "P" (Paid). If paid, it marks the correction as resolved and tracks dollars recovered. This tells us how effective our autonomous corrections actually are.

### Step 3b: Queue Flushing

Several consolidated queues are flushed:

1. **Phone call queue** -- `flush_phone_call_queue()` -- claims that need a live phone call
2. **Write-off approval queue** (Fridays only) -- `flush_writeoff_approval_queue()`
3. **$0 claims** -- `flush_zero_dollar_claims()` -- creates a weekly ClickUp task to Justin listing all $0 billed amount claims that were archived
4. **Suspected duplicates** -- `flush_suspected_duplicates()` -- creates a weekly ClickUp task to Justin listing claims with same member + DOS + program but different claim IDs
5. **Self-learning approval replies** -- `check_self_learning_approvals()` -- checks email for replies from `nm@lifeconsultantsinc.org` approving or rejecting proposed changes

### Step 3c: ClickUp Task Polling

**Import**: `actions/clickup_poller.poll_completed_tasks()`

The system checks ClickUp for tasks that have been completed and have responses (comments from team members). For example, if a "Diagnosis Missing" task was created and someone commented with the correct ICD code, the poller picks it up and acts on it.

### Step 4: Summary and Reporting

The human review queue is saved and a daily summary is posted to ClickUp. The summary includes: ERAs uploaded, claims at start, completed count (broken down by corrections, reconsiderations, write-offs, appeals), autonomous corrections count by type, human review flags, and error count.

**On Fridays**, additional reporting runs:
- **Weekly Performance Scorecard** -- `gap_reporter.generate_performance_report_text()` -- shows 30d/60d/90d/6mo/1yr trends with direction indicators (IMPROVING, STABLE, WORSENING)
- **Training triggers** -- if a staff member hits 3+ of the same gap category in 30 days, a ClickUp task is created for supervisors (Desiree + Nicholas)
- **Write-off threshold alert** -- if weekly write-offs exceed $2,000, an alert is posted requiring review by Nicholas and Desiree

### Step 5: Bank Reconciliation

(Referenced in the orchestrator flow but handled separately -- this is the verification that ERA payments match bank deposits.)

---

## 3. The Decision Tree -- Every Branch

The decision tree lives in `decision_tree/router.py`, in the `ClaimRouter.route()` method. Claims are evaluated in this exact order -- first match wins.

### Node 1: Rural Rate Reduction (RRR)

**Denial code**: `RURAL_RATE_REDUCTION`

**March 2026 rule change**: RRR is NOT a blanket write-off anymore.

- If provider = **NHCS** AND billed amount <= **$19.80** --> **WRITE_OFF** (reason: `rural_rate_reduction_nhcs_under_threshold`)
- Otherwise (urban area providers, or amount > $19.80) --> **RECONSIDERATION** (reason: `rural_rate_reduction_recon_urban_or_over_threshold`)

The constants are `RRR_WRITEOFF_MAX_AMOUNT = 19.80` and `RRR_WRITEOFF_PROGRAM = Program.NHCS`.

### Node 2: Underpayment

**Denial code**: `UNDERPAID`

This has three sub-paths:

**2a. NHCS MHSS overbilled**: If program = NHCS, service = MHSS, and billed amount > (units x $102.72) + $0.01, the system writes off the overage. The correct rate IS $102.72/unit (`NHCS_MHSS_RATE_PER_UNIT = 102.72`), so if we billed more, that overage is on us.

Example: If we billed $120/unit but the correct rate is $102.72/unit, we write off the $17.28/unit difference.

If the paid amount matches the correct total (within $0.01), the claim is skipped as correctly paid.

**2b. NHCS small underpayment**: If program = NHCS and amount <= $19.80 --> **REPROCESS_LAURIS** (auto-reprocess in Lauris)

**2c. All other underpayments**: Auto-submit **RECONSIDERATION** (reason: `underpaid_auto_recon`)

### Node 3: Resubmission Wait

If a claim has `last_followup` set and it has been fewer than **14 days** since that date --> **SKIP** (reason: `resubmitted_wait_14_days`)

This prevents us from immediately re-processing a claim we just fixed. Give the payer time to process our correction.

### Node 4: Reconsideration Status

If claim status = `IN_RECON`:
- If `recon_submitted` date exists and it has been >= **45 days** --> **APPEAL_STEP3** (reason: `recon_no_response_45d`)
- Otherwise --> **SKIP** (recon in progress, not due yet)

### Node 5: Appeal Status

If claim status = `IN_APPEAL`:
- If `appeal_submitted` date exists and it has been >= **45 days** --> **HUMAN_REVIEW** (reason: `appeal_no_response_escalate_dmas`)
- Otherwise --> **SKIP** (appeal in progress, not due yet)

### Node 6: Provider Not Certified

**Denial code**: `PROVIDER_NOT_CERTIFIED`

Two-step process:
- If `last_followup` exists (already retransmitted once) --> **RECONSIDERATION** with DMAS certification letter (reason: `provider_not_certified_recon_dmas_letter`)
- If no previous follow-up (first time) --> **CORRECT_AND_RESUBMIT** (retransmit as-is, reason: `provider_not_certified_retransmit`)

### Node 7: Timely Filing

**Denial code**: `TIMELY_FILING`

**March 2026 rule change**: This is NOT auto-human-review anymore. Two-step process:
- If `last_followup` exists and >= **30 days** since --> **RECONSIDERATION** (reason: `timely_filing_recon_after_30d`)
- Otherwise --> **CORRECT_AND_RESUBMIT** (resubmit first, reason: `timely_filing_resubmit`)

### Node 8: Primary Denial Code Routing Table

The first denial code on the claim is looked up in `_ROUTING_TABLE`:

| Denial Code | Action | Reason |
|---|---|---|
| `INVALID_ID` | CORRECT_AND_RESUBMIT | incorrect_member_id |
| `INVALID_DOB` | CORRECT_AND_RESUBMIT | incorrect_dob |
| `INVALID_NPI` | CORRECT_AND_RESUBMIT | incorrect_npi |
| `INVALID_DIAG` | CORRECT_AND_RESUBMIT | invalid_diagnostic_code |
| `DUPLICATE` | RECONSIDERATION | duplicate |
| `WRONG_BILLING_CO` | LAURIS_FIX_COMPANY | wrong_billing_company |
| `NO_AUTH` | MCO_PORTAL_AUTH_CHECK | no_auth_on_file |
| `AUTH_EXPIRED` | MCO_PORTAL_AUTH_CHECK | auth_expired |
| `NOT_ENROLLED` | RECONSIDERATION | not_enrolled_assessment |
| `RECOUPMENT` | HUMAN_REVIEW | recoupment_detected |
| `NEEDS_CALL` | PHONE_CALL_THURSDAY | unclear_denial |
| `COVERAGE_TERMINATED` | HUMAN_REVIEW | coverage_terminated_check_eligibility |
| `UNLISTED_PROCEDURE` | HUMAN_REVIEW | unlisted_procedure_check_dual_plan |
| `MISSING_NPI_RENDERING` | CORRECT_AND_RESUBMIT | missing_rendering_npi_add_yancey_any_rcsu |
| `DIAGNOSIS_BLANK` | CORRECT_AND_RESUBMIT | diagnosis_blank_clickup_then_fix |
| `EXCEEDED_UNITS` | MCO_PORTAL_AUTH_CHECK | exceeded_units_verify |
| `RECON_DENIED` | APPEAL_STEP3 | reconsideration_denied |
| `NO_RESPONSE_45D` | APPEAL_STEP3 | no_response_45_days |
| `MCO_APPEAL_DENIED` | HUMAN_REVIEW | mco_appeal_denied_escalate_dmas |
| `UNKNOWN` | HUMAN_REVIEW | unknown_denial_code |

**Key detail for MISSING_NPI_RENDERING**: Dr. Yancey's NPI is used as rendering provider for ANY RCSU claim, regardless of which company (KJLN, NHCS, or Mary's Home). See `DR_YANCEY_NPI` in `config/settings.py`.

**Key detail for DIAGNOSIS_BLANK**: The system first attempts to auto-extract the diagnosis from the Mental Health Assessment 3.0 in Lauris (see Section 7). If that fails, a ClickUp task is created -- the claim is NOT submitted without a diagnosis.

**Key detail for COVERAGE_TERMINATED**: The `_smart_human_review()` function in `orchestrator.py` routes these to `handle_coverage_terminated()` which reduces available units to 0 in Lauris and checks eligibility via the Claim.MD API.

### Node 9: Fallback

If the claim has no denial codes but is older than 14 days --> **PHONE_CALL_THURSDAY** (reason: `overdue_no_denial_code`)

Otherwise --> **HUMAN_REVIEW** (reason: `no_routing_match`)

---

## 4. Auth Verification Cascade

When a claim is denied for `NO_AUTH` or `AUTH_EXPIRED`, the `handle_mco_auth_check()` handler in `actions/handlers.py` kicks off a multi-step cascade to find or verify the authorization. The steps are tried in order, stopping at the first success:

### Step 1: Lauris Authorization (authmanage.aspx)

**File**: `lauris/authorization.py`, function `check_lauris_authorization()`

The system:
1. Looks up the consumer's Lauris UID from their Medicaid/record number via `_lookup_uid_from_record_number()` (uses the Record Number search field at `#ctl00_ContentPlaceHolder1_txtRecordNo`)
2. Navigates to `/admin_newui/authmanage.aspx`
3. Enters the consumer UID in the existing key value field
4. Clicks "Load Consumer" then "Search" to populate the auth grid
5. Parses the auth grid (`#ctl00_ContentPlaceHolder1_gridAuth`) -- each row has 14 cells: Start Date, End Date, Vendor, Payor, Authorization #, Record No, Services, plus action icons
6. Finds the auth whose date range covers the claim's DOS
7. Checks for the `viewPerson.png` icon (indicates scanned documents are attached -- but these are `.iif` files which CANNOT be read programmatically)
8. Flags suspicious durations (auths > 3 months for MHSS)

### Step 2: MCO Portal Check

**File**: `mco_portals/auth_checker.py`

If Lauris doesn't have the auth, the system checks the MCO's own portal:
- **Sentara**: Portal login (MFA required -- text to 276-806-4418)
- **United**: Portal check (auths are NOT faxed for United)
- **Aetna/Molina/Anthem**: Via Availity portal
- **Magellan/Humana**: Direct portal or Kepro

### Step 3: Fax Log Database

The system queries the `fax_log` SQLite table (see Section 5) across all 4 fax sources to find prior fax confirmations for this client/MCO combination.

### Step 4: Auto-Refresh Fax Sources

If the fax log doesn't have a match, the system can refresh fax data from the source portals (re-download and re-OCR recent faxes).

### Step 5: Dropbox

Check the Dropbox folder structure for saved authorization documents.

### Step 6: Fax Verify (Live)

If all else fails, the system can look up sent faxes in Nextiva's sent history using `NextivaFaxSession.lookup_sent_fax()` (searches by date and client last name).

### Step 7: ClickUp Task

If no auth is found anywhere, a consolidated ClickUp task is created for the patient with all the details of what was checked and what was not found.

---

## 5. Fax Tracking System

### The 4 Sources

The fax tracking system pulls data from 4 distinct sources:

1. **Lauris Fax Status Report** -- internal Lauris report showing fax submission status
2. **Nextiva nmoyern sent** -- faxes sent from the primary Nextiva account (nmoyern)
3. **Nextiva nmoyern2 sent** -- faxes sent from the secondary account (nmoyern2). **Note: these faxes go to an unknown number (8663465413) -- this needs investigation.**
4. **Nextiva nmoyern received** -- inbound faxes received (auth approvals, denials, etc.)

### PDF Download and OCR

Fax documents are downloaded as PDFs from the Nextiva portal. They're then OCR'd to extract structured data:

**Primary OCR engine**: RapidOCR -- faster for batch processing
**Fallback OCR engine**: Apple Vision (macOS native) -- more accurate for difficult documents but slower

### Data Extracted

From each faxed document, the system extracts:
- **Client name**
- **Entity** (KJLN, NHCS, or Mary's Home)
- **MCO** (which managed care organization)
- **Auth dates** (start and end)
- **Auth number**
- **Status** (approved or denied)

### Early-Stop Optimization

When searching through fax records for a specific client, the system uses early-stop: once it finds a matching record, it stops scanning remaining pages/documents. This saves significant time when processing large batches.

### The fax_log SQLite Table

All extracted fax data is stored in the `fax_log` table in `data/claims_history.db`. This serves as a persistent cache so the system doesn't have to re-OCR documents every run.

---

## 6. Pre-Billing Checks (7 Checks)

**File**: `actions/pre_billing_check.py`

These run BEFORE claims are submitted to Claim.MD for the first time, via `run_pre_billing_checks()`. Each check returns `(passed, message)`.

### Check 1: Diagnosis (`check_diagnosis`)

**What it checks**: Is the diagnosis code present and non-blank?

**Auto-fix**: Attempts to extract the ICD-10 code from the Mental Health Assessment 3.0 in Lauris (navigates to consumer's Documents tab, opens the assessment, scans SSRS ReportViewer pages for "Diagnostic Impression", extracts the F-code from the next line). If found, also updates the Client Face Sheet so it doesn't recur.

**If auto-fix fails**: Blocks the claim and creates a ClickUp task.

### Check 2: Entity/Company (`check_entity`)

**What it checks**: Does the billing entity (company) match what the authorization is under?

**Auto-fix**: If the expected entity can be determined from the program (NHCS/KJLN/MARYS_HOME), updates `billing_region` on the claim. Also checks fax log records for entity information as a secondary source.

**If auto-fix fails**: Blocks the claim.

### Check 3: Authorization (`check_auth`)

**What it checks**: Is the authorization number present?

**Auto-fix**: Would check MCO portal for auth (currently falls through to blocked).

**If auto-fix fails**: Blocks the claim.

### Check 4: Rendering NPI (`check_rendering_npi`)

**What it checks**: For RCSU services across all programs (Mary's Home, NHCS, KJLN), is the rendering NPI populated?

**Auto-fix**: Sets rendering NPI to Dr. Yancey's NPI (`DR_YANCEY_NPI` from settings).

**If auto-fix fails**: Blocks the claim.

### Check 5: Member ID (`check_member_id`)

**What it checks**: Is the member/Medicaid ID present and non-blank?

**Auto-fix**: Would call eligibility API to verify/correct (currently falls through to blocked).

**If auto-fix fails**: Blocks the claim.

### Check 6: NPI (`_check_npi`)

**What it checks**: Is the billing NPI correct for the entity? Uses `ENTITY_NPI_MAP`:
- NHCS: 1588094513
- KJLN: 1235723785
- MARYS_HOME: from settings

**Auto-fix**: Sets NPI to the correct entity NPI.

**If auto-fix fails**: Blocks the claim.

### Check 7: NHCS MHSS Rate (`check_nhcs_mhss_rate`)

**What it checks**: For NHCS + MHSS claims, is the billed amount equal to units x $102.72? (`NHCS_MHSS_RATE_PER_UNIT = 102.72`)

**Auto-fix**: Adjusts `billed_amount` to `units x 102.72` and sets `rate_per_unit`.

Example: A claim for Tiffany Ford with 4 units should be $410.88 (4 x $102.72). If it was billed at $440.00, the system adjusts it down and logs the correction.

**If units are missing**: Warns but does not block -- can't calculate without units.

### Consolidated Reporting

All auto-fixes are logged to:
1. The `pre_billing_log` SQLite table
2. The `autonomous_corrections` table (via `log_autonomous_correction()`)

Blocked claims generate consolidated ClickUp tasks per patient (not per claim -- one task per patient with all their issues listed).

---

## 7. Lauris Integration

### Consumer Search

**File**: `lauris/diagnosis.py`, function `_navigate_to_consumer()`

Lauris uses an autocomplete search. To find a consumer:
1. Navigate to `start_newui.aspx`
2. Type the first 3 characters of the consumer UID into `#txtSearch` (triggers autocomplete dropdown after a 2-second wait)
3. Type remaining characters to narrow results
4. Click the matching autocomplete item
5. Click the consumer's row link in the GridView grid

### Finding Diagnoses

**File**: `lauris/diagnosis.py`, function `extract_diagnosis_from_assessment()`

To find a consumer's diagnosis:
1. Navigate to consumer profile
2. Access the Documents tab inside the `map-iframesec` iframe
3. Clear the start date filter and click Search to show all documents
4. Find "Mental Health Assessment 3.0" link in the `DocsGrid1` table
5. Open the assessment in a new page (it opens outside the iframe as an SSRS ReportViewer)
6. Scan through all pages looking for "Diagnostic Impression"
7. Extract the ICD-10 code from the line after the heading using regex: `(F\d{2}\.\d+)\s*-?\s*(.*)`

Example match: "F25.1 - Schizoaffective Disorder, depressed type" extracts `icd_code="F25.1"` and `description="Schizoaffective Disorder, depressed type"`.

The page number varies per consumer -- the system scans every page. Page navigation uses toolbar buttons (`#ReportToolbar1_Menu_DXI7_Img` for next page, `#ReportToolbar1_Menu_ITCNT5_PageNumber_I` for page input, `#ReportToolbar1_Menu_ITCNT6_PageCount_I` for total pages).

### Checking Authorizations

**File**: `lauris/authorization.py`, function `check_lauris_authorization()`

The auth management page is at `/admin_newui/authmanage.aspx`. The auth grid has these key columns:
- Cell 0: Start Date
- Cell 1: End Date
- Cell 2: Vendor
- Cell 3: Payor
- Cell 4: Authorization #
- Cell 5: Record No
- Cell 6: Services (e.g., "H0046 (78.00)")

The `viewPerson.png` icon indicates scanned documents are attached. These are `.iif` files -- Lauris's proprietary format that CANNOT be read programmatically. We can only note whether the icon exists.

When matching auths to a claim DOS, the system prefers:
1. Auths whose date range covers the DOS
2. Among those, auths whose service code matches
3. Among those, the auth with the narrowest date range (most specific match)

### Posting ERAs

Via the EDI Results page at `/ar/ClosedBillingEDIResults.aspx` -- see Step 1b above.

### Submitting Billing

Via the Close Billing page at `/closebillingnc.aspx` -- see Step 1d above.

For NHCS MHSS claims, the billing page rate is adjusted to $102.72/unit. The function `_adjust_nhcs_mhss_rates()` in `billing_web.py` scans the billing grid for rows containing MHSS procedure codes (H0046, H2014) or "MHSS" text, extracts units, calculates `units x $102.72`, and updates the charge input field.

---

## 8. ClickUp Task Management

**File**: `actions/clickup_tasks.py`

### Consolidated Per-Patient Tasks

The system avoids creating duplicate tasks for the same patient. Before creating a new task:
1. Checks `clickup_patient_tasks` SQLite table for an existing open task for that patient
2. If found, adds a comment to the existing task with the new information
3. If not found, creates a new task

Each task includes:
- What has been done autonomously (with dates)
- What is needed from the human
- Claim history

### Assignee Mapping

Defined in `ASSIGNEE_MAP`:

| Role | Assigned To |
|---|---|
| Default (no role) | Nicholas + Desiree + Justin |
| `billing` | Desiree |
| `bank_verify` | Justin |
| `intake` / `dropbox` | NaTarsha Williams |
| `entity_fix` | Justin |
| `write_off_approval` | Desiree |
| `training` | Desiree + Nicholas |
| `insurance_change` | Nicholas + Desiree |
| `justin` | Justin |
| `nicholas` | Nicholas |

Member IDs: Nicholas=48215738, Desiree=30050728, Justin=48206027, NaTarsha Williams=105978072, Nartarshia McCrey=198206669.

### Task Types Created

1. **Dropbox Save Failures** --> NaTarsha (when auth was submitted via portal but not saved to Dropbox)
2. **Training Flags** --> Desiree + Nicholas (when a staff member hits 3+ of the same gap in 30 days)
3. **Insurance Changes** --> Nicholas + Desiree (when coverage is terminated or MCO changed)
4. **Claims Issues** --> per-patient consolidated tasks (pre-billing blocks, claim issues)
5. **$0 Claims** --> Justin (weekly notification of $0 billed amount claims)
6. **Suspected Duplicates** --> Justin (same member + DOS + program, different claim IDs)
7. **Diagnosis Missing** --> default assignees (when Lauris auto-extraction fails)
8. **Coverage Terminated / 0 Units** --> Nicholas + Desiree

### Due Date

Default due date is 1 business day from creation. The function `_next_business_day()` skips weekends and returns a datetime at 5:00 PM ET.

### Task Polling

The system polls ClickUp for completed tasks with responses. When a team member completes a task and adds a comment (e.g., providing a missing diagnosis code), the poller picks it up in the next run and acts on the response.

---

## 9. Reporting and Self-Learning

### Daily Summary to ClickUp

Every run posts a summary comment to ClickUp via `_post_summary()`. Format:

```
Automated run 03/24/26. ERAs uploaded: 5. Started with 42 outstanding claims.
Completed: 35 (12 corrections, 8 reconsiderations, 10 write-offs, 5 appeals).
Remaining: 7 claims. Autonomous corrections: 15 (entity_fix: 4, npi_fix: 3, ...).
2 claim(s) flagged for human review. #AUTO #03/24/26
```

### Weekly Performance Scorecard (Fridays)

**File**: `reporting/gap_report.py`, function `generate_performance_report_text()`

Posted to ClickUp every Friday. Shows a table with columns: Period, Denials, Resolved, Rate, Write-offs, $/wk WO -- across current week, 30 days, 60 days, 90 days, 6 months, and 1 year.

Direction indicators compare recent (30d) vs older (90d):
- **IMPROVING**: recent metric is 10%+ better
- **WORSENING**: recent metric is 10%+ worse
- **STABLE**: within 10%

Also includes autonomous corrections breakdown by type (entity_fix, npi_fix, member_id_fix, mhss_rate_fix, diagnosis_fix, rendering_npi, resubmitted, recon_submitted) with corrected/resolved counts.

Key rates reported:
- **Auto-Fix Rate**: corrections / total claims processed
- **Resolution Rate**: resolved / total corrections
- **Human Intervention Rate**: claims needing human review / total

### Autonomous Corrections Tracking

**File**: `reporting/autonomous_tracker.py`

Every auto-fix is logged to the `autonomous_corrections` SQLite table with:
- claim_id, client_name, client_id
- correction_type (one of: entity_fix, npi_fix, member_id_fix, mhss_rate_fix, diagnosis_fix, auth_added, rendering_npi_added, resubmitted, reconsideration_submitted)
- correction_detail (what was changed)
- dollars_at_stake (billed amount)
- resolved (0/1, updated later when claim is paid)
- resolved_date

The `check_resolved_corrections()` function queries unresolved entries and checks Claim.MD to see if the claim was accepted/paid. This is how we know our corrections are working.

### Self-Learning Report (Every 10th Run)

**File**: `reporting/self_learning.py`

Every 10th run (tracked in `data/run_counter.txt`), the system generates a comprehensive report:

1. **Decision outcome analysis** (`analyze_decision_outcomes()`): For each action type, what was the success/failure rate? Which actions resolve claims best? Which ones consistently fail?

2. **Pattern identification** (`identify_patterns()`): Finds recurring client denials (same client + same gap, 3+ times), high-volume gap categories (10+ denials), actions with <30% success rate, and monthly trend direction (increasing/decreasing denial volume).

3. **Financial impact estimation** (`estimate_financial_impact()`): For each identified pattern, how many dollars are at stake if the pattern were prevented?

The report is emailed via SMTP (Gmail) to `ss@lifeconsultantsinc.org` and `nm@lifeconsultantsinc.org`.

### Email-Based Approval Workflow

**Only `nm@lifeconsultantsinc.org` can approve self-learning changes.** The constant `APPROVAL_AUTHORIZED_EMAIL` enforces this.

The system checks for email replies to self-learning reports via IMAP (Gmail, port 993). It searches for replies to emails with subject prefix "LCI Claims Automation -- Self-Learning Report". If nm@ replies with approval, the proposed changes are activated. If rejected, they're discarded.

---

## 10. Concerns and Known Issues

### Portal Sessions and MFA

- **Sentara MFA**: Requires a text message to 276-806-4418. If the session expires mid-run, the system cannot re-authenticate without human intervention.
- **Power BI SSO**: Uses Microsoft SSO login flow. "Stay signed in?" prompt is handled, but MFA challenges may require manual intervention.
- **Lauris sessions**: Can expire during long runs. Each handler opens its own `LaurisSession` context manager, so a failure in one doesn't break others, but repeated failures waste time.
- **Nextiva sessions**: The fax portal uses iframes (`xcAppNavStack_frame_send`) which can be fragile.

### Claim.MD Response ID Catching Up

The incremental fetching uses `data/last_responseid.txt`. If this file is deleted or the ID gets out of sync, a full pull (response_id="0") will re-process old claims. This is safe but slow. If the system hasn't run in a while, the response ID may be far behind current data, requiring many API pages to catch up.

### Power BI Position-Based Clicks

The Power BI export uses coordinate-based fallbacks:
- Date inputs: `(290, 141)` and `(370, 141)`
- "Due" slicer: `(277, 470)`
- "Under Payment" slicer: `(277, 484)`
- Middle table: `(500, 340)`

**If Power BI changes its layout, adds a banner, or changes the report design, these coordinates will miss their targets.** The system tries CSS selectors first, but Power BI's DOM structure is inconsistent. When the AR report download fails, ALL Claim.MD denials are processed without AR filtering.

### Lauris .iif Files

The scanned documents attached to authorizations in Lauris are `.iif` files -- Lauris's proprietary format. These CANNOT be read or parsed programmatically. We can only detect whether the `viewPerson.png` icon exists (indicating documents are attached). The actual content must be verified manually.

### OCR Accuracy

RapidOCR is the primary engine (faster for batch processing), with Apple Vision as fallback (more accurate but slower). OCR accuracy varies significantly by document format:
- Clean PDFs from MCO portals: generally reliable
- Faxed documents with poor scan quality: error-prone
- Handwritten annotations: unreliable

Extracted data (client names, auth numbers, dates) should be verified against portal data when possible.

### Nextiva nmoyern2 Faxes

Faxes from the secondary Nextiva account (nmoyern2) are sent to an unknown fax number: **8663465413**. This needs investigation to confirm what number this is and whether faxes are reaching the intended MCO. If it's wrong, authorization requests may be going nowhere.

### Database Locking

All modules share `data/claims_history.db` (SQLite). The autonomous tracker uses `timeout=30` on connections, but if multiple processes run simultaneously (e.g., manual run overlapping with scheduled run), SQLite locking can cause `OperationalError`. The `_ensure_table()` functions silently catch these errors, which means tables might not be created on the first attempt.

### Entity Defaulting

The function `_get_entity_name()` in `fax_refax.py` defaults to "Mary's Home Inc." when the entity can't be determined. **This is a concern** -- if NPI and program are both missing/unknown, all cover letters will say "Mary's Home" which may be incorrect. The pre-billing entity check should catch this before submission, but during the refax workflow it's possible to generate a cover letter with the wrong entity.

**Rule**: Never default to Mary's Home for unknown entities. This default in `_get_entity_name()` should be investigated.

### Billing Rate Inconsistency

There's a minor inconsistency in the codebase: some places reference $9.80/unit for NHCS MHSS (comments in `billing_web.py` and `handlers.py`) while the actual constant is $102.72/unit (`NHCS_MHSS_RATE_PER_UNIT`). The code uses $102.72 correctly in calculations, but the stale comments could confuse a new developer.

### Gap Report Items

Any denial code that maps to `DenialCode.UNKNOWN` gets logged to the `new_denial_patterns` table in the database. These are denial messages from MCOs that don't match any known pattern. They should be reviewed periodically and new patterns should be added to the `parse_denial_codes()` function.

---

## 11. Key Rules to Never Forget

1. **Never use "Life Consultants Inc."** -- always use the specific entity: KJLN Inc., Mary's Home Inc., or New Heights Community Support, LLC. The `_get_entity_name()` function maps NPI/program to entity name.

2. **Never default to Mary's Home for unknown entities.** If the entity can't be determined, flag it for human review rather than guessing.

3. **NHCS MHSS rate is $102.72/unit.** This is enforced in `pre_billing_check.py` (check 7), `billing_web.py` (rate adjustment on Close Billing page), `handlers.py` (correction handler), and `router.py` (underpayment calculation).

4. **RRR threshold: NHCS AND <= $19.80.** Only these specific claims get auto-written off. All other RRR claims go to reconsideration. (`RRR_WRITEOFF_MAX_AMOUNT = 19.80`, `RRR_WRITEOFF_PROGRAM = Program.NHCS`)

5. **United auths are NOT faxed.** United always uses portal submission. The `MCO_AUTH_FAX_NUMBERS` dict in `handlers.py` explicitly comments: "United does NOT use fax -- always portal submission."

6. **Check ALL fax sources.** There are 4 sources (Lauris Fax Status Report + both Nextiva accounts sent + Nextiva received). Missing a source means potentially missing proof of a fax submission.

7. **Always add a note to Claim.MD for every action.** The `add_claim_note()` API call is made after every correction, resubmission, or reconsideration. Notes use the `note_correction()`, `note_reconsideration_submitted()`, etc. formatters from `notes/formatter.py`. Every note includes `#AUTO` and the date stamp.

8. **Only nm@lifeconsultantsinc.org can approve self-learning changes.** The constant `APPROVAL_AUTHORIZED_EMAIL` in `self_learning.py` enforces this. Replies from any other email are ignored.

9. **Denied claims wait 14 days before follow-up.** After a correction is resubmitted, the router skips the claim for 14 days to give the payer time to process. This is in Node 3 of the decision tree.

10. **Rejected claims process immediately.** Rejections mean the claim never reached the payer (format/transmission error), so they should be corrected and resubmitted ASAP. In Step 2b, rejected claims always pass the AR filter.

11. **$0 claims --> archive, weekly ClickUp to Justin.** Claims with $0 billed amount are treated as data quality issues. They're archived in Claim.MD and a consolidated weekly task is sent to Justin.

12. **Always verify against Power BI before processing Claim.MD denials.** The AR cross-reference in Step 2b prevents wasting time on already-resolved claims. If AR data is unavailable, process everything (safe but slower).

13. **Every denial triggers TWO actions: fix the claim AND fix the root cause in Lauris.** After a successful correction, `fix_root_cause()` from `actions/lauris_fixes.py` is called to prevent the same denial from recurring.

14. **Reconsideration no-response at 45 days --> escalate to appeal.** Appeal no-response at 45 days --> escalate to human review (DMAS).

15. **Write-off threshold: $2,000/week.** If exceeded, an alert is posted requiring review by Nicholas and Desiree.

16. **Training trigger: 3+ of same gap category in 30 days.** Creates a training flag task for supervisors.

17. **1.5-second delay between claims.** To avoid hammering MCO portals and Claim.MD with rapid-fire requests.

18. **Irregular ERAs are never auto-processed.** Anthem Mary's, United Mary's, recoupments, and straight Medicaid Mary's require manual handling.

---

*This document was generated from the actual codebase. File paths are relative to the `claims_automation/` directory. If something in this document contradicts what the code does, trust the code.*
