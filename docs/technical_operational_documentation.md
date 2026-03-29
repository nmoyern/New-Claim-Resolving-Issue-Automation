# LCI Claims Automation — Complete Technical & Operational Documentation

**Generated:** 03/23/2026
**System Version:** Production
**Author:** Claims Automation System Documentation

---

## 1. END-TO-END WORKFLOW

The system processes claims through 5 sequential stages, running daily (M-F at 7:00 AM) or on-demand via `bash ~/claims.sh start`.

### Stage 1a: ERA Download & Staging

**Trigger:** Automatic at start of each run
**Data inputs:**
- Claim.MD API key
- `data/era_status.json` (tracks which ERAs have been processed)

**Actions:**
1. Calls Claim.MD API `eralist` endpoint with `new_only=True`
2. Compares returned ERA IDs against `era_status.json` to identify unprocessed ERAs
3. For each unprocessed ERA:
   - Downloads 835 file via API (`era835` endpoint, lowercase `eraid` parameter)
   - Saves to `/tmp/claims_work/eras/era_{id}.835`
   - Classifies ERA using `classify_era()`:
     - Checks against irregular patterns: Anthem Mary's Home, United Mary's Home, Recoupment, Straight Medicaid Mary's Home
     - Standard ERAs proceed to staging
     - Irregular ERAs are logged and skipped
   - Stages standard ERAs to Dropbox: `AR Reports/ERA Files/Pending Upload/YYYY-MM-DD/{mco}_{program}_{era_id}.835`
4. Updates `era_status.json` with processed ERA IDs
5. Posts ClickUp notification listing staged ERAs

**Outputs:** 835 files saved to disk and Dropbox, `era_status.json` updated, ClickUp notification posted

**Duplicate Protection:** Two layers prevent re-processing:
1. `era_status.json` — local tracking of processed ERA IDs
2. `new_only=True` — Claim.MD's server-side tracking of downloaded ERAs
3. EDI Results page check — before posting, scans the posted-files table to skip already-posted ERAs


### Stage 1b: ERA Posting to Lauris

**Trigger:** Automatic after Stage 1a
**Data inputs:**
- Lauris session cookies or credentials (username: `claudeai.lci`)
- Lauris EDI Results page (`/ar/ClosedBillingEDIResults.aspx`)

**Actions:**
1. Logs into Lauris web portal (ASP.NET form: `#ctl00_ContentPlaceHolder1_txtUsername`, `#ctl00_ContentPlaceHolder1_btnLogin`)
2. Handles EULA popup if it appears (first login of day — checks 3 checkboxes + clicks Agree)
3. Navigates to AR Reports > EDI Results page
4. Reads the `ddlEDIFiles` dropdown (contains all available EDI/ERA files — currently 2,698 files)
5. For each file in the dropdown:
   - Checks age: skips files older than 365 days
   - Checks classification: skips irregular ERA patterns
   - Checks posted-files table: skips already-posted files
   - Selects the file from the dropdown
   - Clicks `btnPostFile` ("Post Selected File")
   - Verifies success by checking page body for error indicators ("error", "failed", "unable to post", "exception", "could not process")
   - Takes screenshot on error
6. Posts ClickUp summary: count posted, count skipped (irregular), any errors

**Outputs:** ERAs posted in Lauris, ClickUp notification, screenshots of any errors

**State changes:** Posted files now appear in Lauris AR reports


### Stage 1c: Pre-Billing Checks

**Trigger:** Automatic after ERA posting, before billing submission
**Data inputs:**
- Claims ready for first-time billing (passed as list)
- Claim.MD eligibility API (270/271)
- MCO portal auth data (when available)

**Actions:** For each claim, runs 7 sequential checks:

**Check 1: Diagnosis**
```
IF claim has blank or missing diagnosis:
    → Create ClickUp task to assessor (or Nicholas/Desiree/Justin if unknown)
    → Due: 1 business day
    → Include: claim ID, client name, client ID, what's needed
    → BLOCK claim from billing
    → FAIL
ELSE:
    → PASS
```

**Check 2: Billing Entity**
```
IF claim NPI is empty or doesn't match known entities:
    TRY: Match program to expected entity (NHCS→NHCS, KJLN→KJLN, MARYS_HOME→MARYS_HOME)
    IF mismatch detected:
        → Auto-fix billing_region to match program
        → Log: "Pre-billing fix: entity corrected"
        → PASS (auto-fixed)
    IF NPI completely unknown:
        → Create ClickUp to Justin
        → BLOCK claim
        → FAIL
```

**Check 3: Authorization Number**
```
IF claim has no authorization number:
    → Create ClickUp: "Auth needed for {client} before billing"
    → Include: claim ID, client ID
    → BLOCK claim
    → FAIL
```

**Check 4: Rendering NPI (RCSU Services)**
```
IF claim service code = RCSU:
    IF rendering NPI is missing or blank:
        → Add Dr. Tiffinee Yancey (NPI: 1619527645)
        → Log: "Pre-billing fix: rendering NPI added for RCSU"
        → PASS (auto-fixed)
```

**Check 5: Member ID**
```
IF claim member ID is blank or flagged as invalid:
    → Create ClickUp: "Member ID verification needed for {client}"
    → Include: client ID
    → BLOCK claim
    → FAIL
```

**Check 6: NPI Validation**
```
IF claim NPI is missing:
    → Auto-fix with entity NPI from ENTITY_NPI_MAP
    → PASS (auto-fixed)
IF claim NPI doesn't match entity:
    → Auto-fix
    → PASS (auto-fixed)
```

**Check 7: NHCS MHSS Rate**
```
IF claim program = NHCS AND service_code = MHSS:
    correct_amount = units x $102.72
    IF billed_amount != correct_amount:
        → Adjust billed_amount to correct_amount
        → Set rate_per_unit = 102.72
        → Log adjustment
        → PASS (auto-fixed)
    IF no units data:
        → WARN (don't block, just log)
```

**Outputs:** Claims either PASS (proceed to billing), auto-FIXED (corrected and proceed), or BLOCKED (held until ClickUp resolved)

**State changes:** Pre-billing issues logged to `pre_billing_log` SQLite table. Blocked claims get ClickUp tasks assigned to Nicholas, Desiree, and Justin with 1 business day due date.


### Stage 1d: Billing Submission

**Trigger:** Automatic after pre-billing checks (runs whenever called, no day restriction)
**Data inputs:**
- Lauris Close Billing page (`/closebillingnc.aspx`)
- Region dropdown: KJLN Inc (88), Mary's Home Inc (97), New Heights Community Support (89)

**Actions:**
1. Login to Lauris
2. Navigate to Close Billing page
3. For each region (KJLN, NHCS, Mary's Home):
   a. Select region from `ddlRegion` dropdown
   b. Select billing date on calendar
   c. Click `btnRefresh` to load billing items
   d. **NHCS only:** Run MHSS rate adjustment — scan grid for H0046/H2014 procedure codes, set charge to units x $102.72
   e. Click "Close Reconciled Billing Items" (`btnClose`)
4. Post ClickUp summary: regions processed, all MCOs included

**Outputs:** Claims submitted to MCOs via Lauris, ClickUp notification

**Key Rules:**
- All MCOs billed (Aetna exclusion removed March 2026)
- No day-of-week restriction
- NHCS MHSS claims automatically adjusted to $102.72/unit before submission


### Stage 2: Denied Claim Retrieval

**Trigger:** Automatic after billing
**Data inputs:**
- Claim.MD API key
- `data/last_responseid.txt` (incremental fetching marker)

**Actions:**
1. Calls Claim.MD API `response` endpoint with stored response ID (incremental — only new responses)
2. Filters responses:
   - Status "R" (Rejected) or "D" (Denied) or "4" or "22" → process
   - Status "A" (Accepted) → skip
3. For each denied/rejected response:
   - Converts to `Claim` dataclass via `_raw_to_claim()`
   - Extracts: claim_id, patient name, member ID, DOS, payer → MCO mapping, denial messages, billed amount, NPI → program mapping, procedure code, units, service code
   - Parses denial messages against 22 regex patterns to determine `DenialCode`
   - Logs unrecognized patterns to `new_denial_patterns` table
4. Filters out:
   - Claims with DOS older than 365 days
   - $0 claims (archived, weekly ClickUp to Justin)
   - Duplicate claim IDs (deduped via `seen_ids` set)
   - Suspected duplicates: same member + DOS + program, different PCN (weekly ClickUp to Justin)
5. Saves new `last_responseid` for next run

**Outputs:** List of `Claim` objects ready for routing

**Payer ID Mapping** (complete coverage):
| Payer ID | MCO |
|----------|-----|
| 54154, VAPRM, 00453 | Sentara |
| 128VA | Aetna |
| MCCVA | Molina |
| 87726, 77350 | United |
| 00423, 00923, SB923 | Anthem |
| SPAYORCODE | DMAS |
| 31140, 61101 | Humana |
| 38217 | Magellan |


### Stage 3: Claim Routing & Resolution

**Trigger:** Automatic after claim retrieval
**Data inputs:**
- List of `Claim` objects from Stage 2
- Decision tree rules (see Section 2)

**Actions:**
1. For each claim, `ClaimRouter.route()` determines the `ResolutionAction`
2. `orchestrator.dispatch()` maps action to handler function
3. Handler executes the resolution (see Section 2 for full decision tree)
4. `ResolutionResult` logged to `claim_history` database
5. Gap category determined and logged to `gap_report` database

**Dispatch Map:**
| ResolutionAction | Handler Function |
|-----------------|------------------|
| ERA_UPLOAD | `handle_era_upload()` |
| CORRECT_AND_RESUBMIT | `handle_correct_and_resubmit()` |
| RECONSIDERATION | `handle_reconsideration()` |
| MCO_PORTAL_AUTH_CHECK | `handle_mco_auth_check()` |
| LAURIS_FAX_VERIFY | `handle_lauris_fax_verify()` |
| LAURIS_FIX_COMPANY | `handle_lauris_fix_company()` |
| REPROCESS_LAURIS | `handle_reprocess_lauris()` |
| WRITE_OFF | `handle_write_off()` |
| APPEAL_STEP3 | `handle_appeal()` |
| PHONE_CALL_THURSDAY | `handle_phone_call_flag()` |
| HUMAN_REVIEW | `handle_human_review()` |
| SKIP | (no action — logged only) |

**Outputs:** Resolution results, Claim.MD notes, Lauris fixes, ClickUp tasks, gap report entries


### Stage 3b: Queue Flushing

**Trigger:** After all claims processed

**Actions:**
1. **Phone call queue** — Creates consolidated ClickUp task grouping all claims needing MCO calls, organized by client
2. **Write-off approval queue** (Fridays only) — Creates weekly ClickUp task to Desiree listing all non-RRR write-offs needing approval
3. **$0 claims flush** — Creates weekly ClickUp to Justin listing archived $0 claims
4. **Suspected duplicates flush** — Creates weekly ClickUp to Justin listing same-member+DOS+program claims with different IDs
5. **Self-learning approval check** — Checks email for nm@ replies approving proposed changes


### Stage 3c: ClickUp Task Polling

**Trigger:** Every run, after queue flushing

**Actions:**
1. Queries SQLite for all open automation-created tasks
2. For each open task, calls ClickUp API to check status
3. If task is marked "complete" or "closed":
   - Extracts human response from most recent comment (ignores automation-generated comments)
   - Routes to appropriate handler based on task type:

| Task Type | Response Action |
|-----------|----------------|
| bank_verify | Extract PAY-XXXXXX code, mark payment reconciled |
| entity_fix | Extract entity name (KJLN/NHCS/Mary's Home), correct claim via API |
| diagnosis_missing | Extract ICD-10 code, add to claim, resubmit |
| write_off_approval | Detect approved/denied, process or hold batch |
| intake_issue | Log resolution, add note to claim |
| insurance_change | Log completion |
| generic | Log completion only |

4. Marks task as resolved in SQLite with response text, responder, and date


### Stage 4: Summary & Reporting

**Trigger:** After all processing complete

**Actions:**
1. Posts daily summary to ClickUp:
   - ERAs uploaded count
   - Claims processed (corrections, recons, write-offs, appeals)
   - Claims remaining
   - Human review flags
   - Error count
2. **Fridays only:**
   - Posts weekly Performance Scorecard (30/60/90d/6mo/1yr trends)
   - Checks training triggers (3+ same gap per staff in 30 days) → creates ClickUp tasks
   - Checks write-off threshold ($2,000/week) → alerts Nicholas + Desiree
3. **Every 10th run:**
   - Generates Self-Learning Report
   - Emails to ss@ and nm@lifeconsultantsinc.org
   - Proposes improvements with financial impact estimates


### Stage 5: Bank Reconciliation

**Trigger:** Can be run independently via `bash ~/claims.sh reconcile`

**Actions:**
1. Get new ERA payments from Claim.MD API
2. Track each payment with unique code (PAY-XXXXXX):
   - Maps NPI to bank: KJLN→Wells Fargo, Mary's Home→Southern Bank, NHCS→Bank of America
3. Check email (IMAP) for manual payment confirmations:
   - Subject format: `PAY-XXXXXX_PAID_YYYY-MM-DD`
   - Security: Only from @lifeconsultantsinc.org + CC nm@
   - Handles PAID, WRITEOFF, and CANCEL commands
4. Auto-verify against bank portal deposits (when sessions available):
   - Logs into each bank portal
   - Pulls recent deposits (14 days)
   - Matches by amount (within $0.01) and date (within 5 days)
5. Escalate unreconciled payments after 7 business days:
   - Creates ClickUp task assigned to Justin with 1 business day due date
6. Generates Claim Bank Reconciliation Report

**Report Format:**
```
CLAIM BANK RECONCILIATION REPORT — MM/DD/YY
=================================================================
SUMMARY:
  Pending verification:  X payments, $X,XXX.XX
  Reconciled (verified): X payments, $X,XXX.XX
  Written off:           X payments, $X,XXX.XX
  Escalated to human:    X

BY BANK:
  Wells Fargo (KJLN): X pending ($X,XXX.XX), X reconciled
  Southern Bank (Mary's Home): X pending, X reconciled
  Bank of America (NHCS): X pending, X reconciled

UNRECONCILED PAYMENTS:
Code            Payer                Bank            Amount     Date         Esc
-----------------------------------------------------------------
PAY-A3F7B2      Sentara              Wells Fargo     $1,234.56  2026-03-15
```

---

## 2. DECISION TREE / LOGIC MAP

The `ClaimRouter.route()` method evaluates claims in strict priority order. The first matching rule wins.

### Node 1: Rural Rate Reduction (RRR)
```
CONDITION: DenialCode.RURAL_RATE_REDUCTION in claim.denial_codes?
TYPE: Rule-based

  YES →
    CONDITION: Is provider NHCS AND billed_amount <= $19.80?
      YES → WRITE_OFF (standard RRR for rural area provider)
      NO  → RECONSIDERATION (urban providers should be paid as billed)
  NO → Proceed to Node 2
```

### Node 2: Underpayment
```
CONDITION: DenialCode.UNDERPAID in claim.denial_codes?
TYPE: Rule-based

  YES →
    CONDITION: Is NHCS + MHSS + units > 0?
      YES →
        correct_total = units x $102.72
        IF billed_amount > correct_total:
          → WRITE_OFF the overage (billed above correct rate)
        IF paid_amount matches correct_total:
          → SKIP (paid correctly at $102.72/unit)

    CONDITION: Is NHCS AND billed_amount <= $19.80?
      YES → REPROCESS_LAURIS (auto-reprocess)

    ELSE → RECONSIDERATION (submit recon for all other underpaid)
  NO → Proceed to Node 3
```

### Node 3: Resubmission Wait Period
```
CONDITION: Was claim resubmitted (last_followup set) within 14 days?
TYPE: Rule-based (14-day cooldown after automation acts)

  YES → SKIP (wait for payer to process resubmission)
  NO  → Proceed to Node 4

NOTE: New denials/rejections are processed IMMEDIATELY.
      The 14-day wait only applies AFTER the automation has
      already corrected and resubmitted a claim.
```

### Node 4: Reconsideration Status
```
CONDITION: Is claim status IN_RECON?
TYPE: Rule-based

  YES →
    CONDITION: Has recon been pending 45+ days?
      YES → APPEAL_STEP3 (escalate to formal appeal)
      NO  → SKIP (recon still in progress, check again next run)
  NO → Proceed to Node 5
```

### Node 5: Appeal Status
```
CONDITION: Is claim status IN_APPEAL?
TYPE: Rule-based

  YES →
    CONDITION: Has appeal been pending 45+ days?
      YES → HUMAN_REVIEW (DMAS escalation decision needed)
      NO  → SKIP (appeal still in progress)
  NO → Proceed to Node 6
```

### Node 6: Provider Not Certified
```
CONDITION: DenialCode.PROVIDER_NOT_CERTIFIED in denial_codes?
TYPE: Rule-based with state tracking

  YES →
    CONDITION: Was claim previously retransmitted (last_followup set)?
      YES → RECONSIDERATION with DMAS certification letter
      NO  → CORRECT_AND_RESUBMIT (retransmit first attempt)
  NO → Proceed to Node 7
```

### Node 7: Timely Filing
```
CONDITION: DenialCode.TIMELY_FILING in denial_codes?
TYPE: Rule-based with state tracking

  YES →
    CONDITION: Previously resubmitted AND 30+ days have passed?
      YES → RECONSIDERATION (resubmission didn't resolve it)
      NO  → CORRECT_AND_RESUBMIT (try resubmitting first)
  NO → Proceed to Node 8
```

### Node 8: Primary Denial Code Routing
```
The first denial code in claim.denial_codes is used as the routing key.

ROUTING TABLE:
  INVALID_ID            → CORRECT_AND_RESUBMIT (fix member ID via eligibility API)
  INVALID_DOB           → CORRECT_AND_RESUBMIT (fix DOB via eligibility API)
  INVALID_NPI           → CORRECT_AND_RESUBMIT (fix NPI to entity NPI)
  INVALID_DIAG          → CORRECT_AND_RESUBMIT (fix diagnosis code)
  DUPLICATE             → RECONSIDERATION (verify not actually duplicate)
  WRONG_BILLING_CO      → LAURIS_FIX_COMPANY (fix in Lauris facesheet)
  NO_AUTH               → MCO_PORTAL_AUTH_CHECK (verify auth in portal)
  AUTH_EXPIRED          → MCO_PORTAL_AUTH_CHECK (check if auth was renewed)
  NOT_ENROLLED          → RECONSIDERATION (assessment/eligibility issue)
  COVERAGE_TERMINATED   → HUMAN_REVIEW (check eligibility, zero units)
  RECOUPMENT            → HUMAN_REVIEW (complex financial adjustment)
  RECON_DENIED          → APPEAL_STEP3 (escalate to formal appeal)
  NO_RESPONSE_45D       → APPEAL_STEP3 (no response escalation)
  NEEDS_CALL            → PHONE_CALL_THURSDAY (unclear denial)
  UNLISTED_PROCEDURE    → HUMAN_REVIEW (check dual plan)
  MISSING_NPI_RENDERING → CORRECT_AND_RESUBMIT (add Dr. Yancey NPI)
  DIAGNOSIS_BLANK       → CORRECT_AND_RESUBMIT (ClickUp first, then fix)
  EXCEEDED_UNITS        → MCO_PORTAL_AUTH_CHECK (verify approved units)
  MCO_APPEAL_DENIED     → HUMAN_REVIEW (DMAS escalation)
  UNKNOWN               → HUMAN_REVIEW (unrecognized denial)
```

### Node 9: Fallback
```
IF claim.age_days > 14 AND no denial codes matched:
  → PHONE_CALL_THURSDAY (overdue, needs investigation)

ELSE:
  → HUMAN_REVIEW (no routing match — requires manual analysis)
```

### Handler Sub-Decision Trees

#### MCO Auth Check (`handle_mco_auth_check`)
```
1. Check MCO portal for auth:
   - Sentara: Menu → My Members → search by last name → View Member Abstract
   - United: Prior Authorizations → search by DOS range (±30 days)
     NOTE: United auths are NOT faxed. If not found → urgent ClickUp
   - Molina: Availity → Payer Spaces → Molina → Prior Auths
   - Anthem: Availity → Patient Registration → Auth & Referrals
     NOTE: Check ALL 3 orgs (Mary's Home, KJLN, NHCS)
   - Aetna: Availity → Patient Registration → Auth & Referrals
   - Kepro/DMAS: Cases → Case Type "UM" → date range search

2. IF auth found AND already on claim:
   → Submit reconsideration directly with auth evidence

3. IF auth found BUT not on claim:
   → Correct claim with auth number → resubmit

4. IF auth NOT found in portal:
   → Check Dropbox for saved auth confirmation
   → Check Lauris fax history for SRA submission
   → Check Nextiva fax history for SRA submission
   → IF still not found: Create ClickUp task to Justin
```

#### Entity Determination Cascade (`_determine_entity_or_clickup`)
```
Step 1: Check MCO portal auth → entity from auth record
Step 2: Check Lauris fax history → entity from SRA
Step 3: Check Nextiva fax history → entity from fax details
Step 4: Check Dropbox → entity from file path (KJLN/NHCS/Mary's in path)
Step 5: If ALL fail → Create ClickUp to Justin (assigned, 1 business day)
        → BLOCK claim until entity determined
```

#### Correction Builder (`_build_corrections`)
```
FOR each denial code on claim:
  INVALID_ID      → corrections["member_id"] = (lookup via eligibility API)
  INVALID_DOB     → corrections["dob"] = (lookup via eligibility API)
  INVALID_NPI     → corrections["npi"] = entity NPI from mapping
  WRONG_BILLING_CO → corrections["billing_region"] = (inferred from program)
  MISSING_NPI_RENDERING → corrections["rendering_npi"] = Dr. Yancey NPI
  PROVIDER_NOT_CERTIFIED → (retransmit as-is)
  DIAGNOSIS_BLANK → (route to ClickUp, do NOT submit without diagnosis)

THEN: Apply NHCS MHSS rate correction if applicable
THEN: Submit corrections via Claim.MD API (modify endpoint)
THEN: Fix root cause in Lauris (dual-action rule)
```

---

## 3. SELF-MONITORING FEATURES

### Metrics Tracked

**Per-Run Metrics (DailyRunSummary):**
- ERAs uploaded
- Claims at start / completed / remaining
- Write-offs count
- Reconsiderations submitted
- Corrections made
- Appeals submitted
- Human review flags
- Error count

**Gap Report Metrics (SQLite — persistent across runs):**
- Denial count and dollars by period (week, 30d, 60d, 90d, 6mo, 1yr)
- Resolution rate (resolved / total)
- Write-off count and dollars
- Timely filing losses (zero-tolerance KPI)
- Recurring client denials (same type within 60 days)
- Training triggers (staff member with 3+ same gap in 30 days)
- ERA upload lag (days between receipt and posting)
- Dropbox save failures
- By MCO breakdown
- By gap category breakdown

**Autonomous Corrections Tracking:**
- Pre-billing auto-fixes by type (entity, NPI, member ID, MHSS rate)
- Claims resolved autonomously (no human intervention)
- Dollars recovered autonomously
- Human intervention rate vs autonomous rate
- Trend: is auto-fix rate increasing over time?

### Performance Scorecard (Weekly on Fridays)

```
PERFORMANCE SCORECARD — MM/DD/YY
============================================================

OVERALL DIRECTION:
  Denials: ^ IMPROVING / = STABLE / v WORSENING
  Resolution Rate: ^ IMPROVING / = STABLE / v WORSENING
  Writeoffs: ^ IMPROVING / = STABLE / v WORSENING

------------------------------------------------------------
Period          Denials   Resolved     Rate   Write-offs  $/wk WO
------------------------------------------------------------
This Week           X          X     XX.X%   $X,XXX.XX  $X,XXX.XX
30 Days             X          X     XX.X%   $X,XXX.XX  $X,XXX.XX
60 Days             X          X     XX.X%   $X,XXX.XX  $X,XXX.XX
90 Days             X          X     XX.X%   $X,XXX.XX  $X,XXX.XX
6 Months            X          X     XX.X%   $X,XXX.XX  $X,XXX.XX
1 Year              X          X     XX.X%   $X,XXX.XX  $X,XXX.XX
------------------------------------------------------------

AUTONOMOUS CORRECTIONS (no human intervention):
  This Week: X claims resolved (XX.X% auto rate), $X,XXX recovered
  30 Days: X claims resolved (XX.X% auto rate), $X,XXX recovered
  90 Days: X claims resolved (XX.X% auto rate), $X,XXX recovered
    Pre-billing auto-fixes: X (entity: X, npi: X, mhss_rate: X)
```

**Direction Calculation:**
```
Compare 30-day average vs 90-day average:
  IF 30d_avg < 90d_avg * 0.9 → "IMPROVING" (10%+ reduction)
  IF 30d_avg > 90d_avg * 1.1 → "WORSENING" (10%+ increase)
  ELSE → "STABLE"
```

### Anomaly Detection & Alerts

| Threshold | Trigger | Action |
|-----------|---------|--------|
| Write-offs > $2,000/week | Friday check | Alert to Nicholas + Desiree via ClickUp |
| Training trigger: 3+ same gap/staff/30 days | Every run | ClickUp to supervisor (Desiree + Nicholas) |
| $0 claims detected | Every run | Archive + weekly ClickUp to Justin |
| Suspected duplicate claims | Every run | Archive + weekly ClickUp to Justin |
| New unrecognized denial pattern | Every run | Logged to `new_denial_patterns` table |
| Claim with empty client name | Every run | Flag for human review, use PCN as fallback |
| Claim with empty denial message | Every run | Route to HUMAN_REVIEW |

### Audit Logging

Every action is logged to multiple destinations:
1. **SQLite `claim_history`** — Full action history per claim with timestamps
2. **SQLite `gap_report`** — Gap category, resolution, dollar amounts
3. **SQLite `pre_billing_log`** — Pre-billing issues, auto-fixes, blocked claims
4. **Claim.MD notes** — Standardized note format: `[Action. Found. Done. Fix. Gap. Next.] #AUTO #MM/DD/YY`
5. **ClickUp comments** — Daily summaries and task updates
6. **Structured logs** — `structlog` with JSON-formatted log entries
7. **Screenshots** — Saved on browser automation errors to `logs/screenshots/`

---

## 4. SELF-IMPROVEMENT / ADAPTIVE LOGIC

### Feedback Loops

**LOOP 1: Action Outcome Tracking**
```
Action taken → Outcome observed next run → Success/failure logged
  → Resolution rate per action type calculated
  → Failing actions flagged in self-learning report
  → Pattern detector identifies recurring failures
```

**LOOP 2: Gap Report → Training Trigger → Staff Improvement**
```
Gap logged → Recurrence detected (same type, same staff, 30 days)
  → Training trigger fires → ClickUp to supervisor
  → Training delivered → Gap frequency monitored next period
```

**LOOP 3: Pattern Detection → Rule Proposal → Human Review → Implementation**
```
New denial pattern detected (UNKNOWN denial code)
  → Logged to new_denial_patterns table
  → Self-learning report identifies pattern after 10 runs
  → Proposes new regex pattern with financial impact estimate
  → Human reviews and approves via email reply ("approved")
  → Pattern added to code by developer
```

**LOOP 4: Pre-Billing Check → Denial Prevention → Cost Avoidance**
```
Pre-billing catches issue → Issue fixed before billing
  → Denial prevented → Tracked as "prevented denial"
  → Self-learning report shows cost avoidance
  → Autonomous correction rate trends upward
```

### Self-Learning Report (Every 10th Run)

**Sections:**
1. **Decision Outcomes** — Every action type with success/failure rates
2. **What's Working** — Actions with >=60% resolution rate
3. **What's Not Working** — Actions with <50% resolution rate
4. **Recurring Patterns** — Clients with repeated denials, high-volume gap categories, failing action types, trend changes
5. **Financial Impact** — Estimated preventable dollars per pattern, top 5 improvement opportunities
6. **Efficiency Improvements** — Recommended actions to avoid or expand

**Report Delivery:**
- Emailed to ss@lifeconsultantsinc.org and nm@lifeconsultantsinc.org
- Via Gmail SMTP from ea@lifeconsultantsinc.org (app password authenticated)

### Approval Workflow for Changes

```
1. Self-learning report sent with proposed changes
2. Each proposal tracked in SQLite `proposals` table with unique ID
3. nm@lifeconsultantsinc.org may reply:
   - "approved" → Proposal status updated to approved
   - Clarification question → Logged for manual follow-up
   - Any other sender → REJECTED (security: nm@ only)
4. ONLY nm@lifeconsultantsinc.org can approve changes
5. No autonomous rule changes — system proposes, human approves
6. Developer implements approved changes in code
```

### Guardrails Against Unchecked Changes

1. **No autonomous rule changes** — The system identifies patterns and proposes changes but DOES NOT modify its own routing rules, regex patterns, or thresholds
2. **Report before action** — Self-learning reports emailed BEFORE any changes would be implemented
3. **Financial impact disclosure** — Every proposed change includes estimated dollar impact
4. **Human override logging** — All ClickUp task completions and email commands logged with who, when, what
5. **Version tracking** — All code in project files, system does not modify its own source code
6. **DRY_RUN mode** — Full dry run capability for testing without affecting real data
7. **Email security** — Only nm@ can approve changes; all other emails logged and ignored

---

## 5. EDGE CASES & EXCEPTION HANDLING

### Incomplete Claims

**Empty NPI:**
```
→ DO NOT default to any entity
→ Check MCO portals for auth → determine entity from auth
→ Check Lauris fax history → determine entity from SRA
→ Check Nextiva fax history → determine entity from fax
→ Check Dropbox → determine entity from saved auth
→ IF still unknown → Create ClickUp to Justin
→ BLOCK claim until entity determined
```

**Empty member ID:**
```
→ Try eligibility API lookup by name + DOB + payer
→ IF found → Use API-returned member ID
→ IF not found → Create ClickUp, BLOCK claim
```

**$0 charge:**
```
→ DO NOT process
→ Flag as data quality issue
→ Archive claim in Claim.MD
→ Weekly ClickUp notification to Justin (Operations Manager)
→ Group all $0 claims into single weekly task
```

**Empty client name:**
```
→ Use PCN (Patient Control Number) as fallback identifier
→ Flag for human review: "Empty client name — manual verification needed"
→ Route to HUMAN_REVIEW
```

**Empty denial message:**
```
→ All denial codes = UNKNOWN
→ Route to HUMAN_REVIEW
→ Log: "No denial message provided by MCO"
```

### Duplicate Claims

**Same claimmd_id appears multiple times:**
```
→ Deduplicated: first occurrence processed, subsequent skipped
→ Dedup tracked via set() in memory during each run
```

**Same member + DOS + program, different claim ID (different PCN/LCN):**
```
→ Flag as suspected duplicate
→ Archive the duplicate, skip processing
→ Weekly grouped ClickUp notification to Justin
→ Justin reviews and confirms which to keep
```

**DUPLICATE denial code received:**
```
→ Pre-check: query Claim.MD for matching member ID + DOS + status "A"
→ IF matching accepted claim found → claim was already paid, archive duplicate
→ IF NOT found → Submit reconsideration (claim is not actually duplicate)
```

### Payer-Specific Variations

**United:**
- Auths are NOT faxed — always portal submission
- If auth not found in portal → create URGENT ClickUp (do NOT check fax)
- Reconsiderations submitted via TrackIt portal (not Claim.MD)
- Search DOS range: ±30 days (not just 7 days)

**Anthem:**
- Check ALL 3 organizations in Availity (Mary's Home, KJLN, NHCS)
- Auth could be under any company
- Payer selection: "Anthem - VA"

**Aetna:**
- Payer selection: "Aetna Better Health All Plans and NJ-VA MAPD-DSNP"
- No exclusions from billing (removed March 2026)

**Sentara:**
- SMS MFA to 276-806-4418 (Nick's phone)
- Largest claim volume (~9,218 claims)

**DMAS/Kepro:**
- Uses Microsoft Azure AD login
- Cases → Case Type "UM"
- View procedures for auth details

**Magellan:**
- Does NOT always require Thursday phone call first
- Route through normal decision tree

### API/System Failures

**Claim.MD API timeout:**
```
→ Timeout set to 120 seconds (increased from 30s for large responses)
→ On failure: log error, continue with other operations
→ Incremental fetching ensures no data loss — resume from last response ID
```

**Lauris login failure:**
```
→ Try session cookie restore first
→ If cookies expired: full login with ASP.NET form
→ Handle EULA popup (first login of day)
→ On failure: screenshot saved, error logged, skip Lauris operations
→ Fallback message: "Run bash ~/claims.sh lauris to login manually"
```

**MCO portal login failure:**
```
→ Session cookie strategy (grab from real Chrome via CDP)
→ MFA routing: manual (5 min wait), TOTP, Duo push, SMS
→ On failure: skip portal check, proceed to fax/Dropbox verification
→ Create ClickUp if all verification methods fail
```

**Nextiva fax failure:**
```
→ Form rendered in iframe (xcAppNavStack_frame_send)
→ Multi-step wizard: Recipient → Attachments → Preview → Send → Confirmation
→ On failure: screenshot saved, error logged
→ Fallback: manual fax via Nextiva web portal
```

**Bank portal failure:**
```
→ Bank auto-verification is optional — if portals fail, email confirmation still works
→ 7-day escalation to Justin via ClickUp regardless of portal status
→ On failure: log warning, continue with email-based confirmation
```

### Low Confidence Scenarios

```
IF denial code = UNKNOWN:
  → Route to HUMAN_REVIEW (cannot determine appropriate action)
  → Log raw denial message to new_denial_patterns table
  → Self-learning report will propose pattern recognition

IF claim has contradictory data (e.g., paid but also denied):
  → Route to HUMAN_REVIEW
  → Note: "Contradictory claim data detected"

IF eligibility API returns ambiguous results:
  → Use existing claim data (don't override)
  → Flag for human verification via ClickUp

IF multiple denial codes on same claim:
  → First code wins (priority routing)
  → All codes logged for pattern analysis
```

---

## 6. HUMAN-IN-THE-LOOP DESIGN

### Decisions Requiring Human Review

| Scenario | Why | Assigned To | Due |
|----------|-----|-------------|-----|
| Diagnosis code blank | Cannot bill without diagnosis — must come from assessment | Assessor (or Nicholas/Desiree/Justin) | 1 business day |
| Entity unknown after full cascade | All automated checks exhausted | Justin | 1 business day |
| Coverage terminated | Requires supervisor notification + unit zeroing | Nicholas + Desiree | 1 business day |
| MCO appeal denied | DMAS escalation decision | Nicholas + Desiree + Justin | 1 business day |
| Recoupment | Complex financial adjustment | Nicholas + Desiree + Justin | 1 business day |
| Unlisted procedure code | May need dual plan verification | Nicholas + Desiree + Justin | 1 business day |
| Unknown denial code | System cannot route | Nicholas + Desiree + Justin | 1 business day |
| Auth never submitted | Initial SRA needs to be sent | Nicholas + Desiree + Justin | 1 business day |
| Write-off approval (non-RRR) | Financial approval required | Desiree | 1 business day |
| Bank payment unreconciled 7+ days | Need manual bank verification | Justin | 1 business day |
| $0 claims detected | Data quality investigation | Justin | 1 business day |
| Suspected duplicate claims | Confirm which claim to keep | Justin | 1 business day |
| Insurance change | Coach must contact client | Nicholas + Desiree | 1 business day |
| Training trigger (3+ same gap) | Staff coaching needed | Desiree + Nicholas | 1 business day |

### ClickUp Task Prioritization

All automation-created tasks include:
- **Assignees** — Specific team members based on task type (see ASSIGNEE_MAP)
- **Due date** — 1 business day from creation (default)
- **Priority** — High (2) for most, Normal (3) for phone calls and reports
- **Description** — Includes claim ID, client name, client ID, what's been done, what's needed

**Consolidation:** Tasks are grouped per patient — if multiple claims fail for the same patient, one task is created (or comments added to existing task).

### ClickUp Member IDs

| Person | Role | ClickUp ID |
|--------|------|------------|
| Nicholas Moyer | CEO | 48215738 |
| Desiree Whitehead | Billing Supervisor | 30050728 |
| Justin Overdorf | Operations Manager | 48206027 |
| NaTarsha Williams | Intake/Admin | 105978072 |

**Default Assignees:** When role is unknown, tasks go to Nicholas + Desiree + Justin.

**Role-Based Routing:**
| Role | Assigned To |
|------|------------|
| billing | Desiree |
| bank_verify | Justin |
| intake / dropbox | NaTarsha Williams |
| entity_fix | Justin |
| write_off_approval | Desiree |
| training | Desiree + Nicholas |
| insurance_change | Nicholas + Desiree |

### How Human Overrides Feed Back

1. **ClickUp task completion** → `clickup_poller.py` detects completion, extracts response from comments, routes to appropriate handler (entity fix, diagnosis add, write-off approval, bank verification, etc.)

2. **Email commands** (bank reconciliation) → IMAP monitoring checks for:
   - `PAY-XXXXXX_PAID_YYYY-MM-DD` → marks payment reconciled
   - `WRITEOFF` with Excel attachment → processes PCN write-off list
   - `CANCEL` → logs for manual review

3. **Self-learning approval** → nm@ replies with "approved" → proposal status updated in SQLite

4. **All overrides logged** — who, when, what action, in both SQLite and Claim.MD notes

---

## SYSTEM ARCHITECTURE SUMMARY

```
                         bash ~/claims.sh start
                                |
                         orchestrator.py
                                |
            +-------------------+-------------------+
            |                   |                   |
     ERA Download          Claim Pull          Bank Recon
     (claimmd_api)        (claimmd_api)       (bank_reconciler)
            |                   |                   |
     ERA Posting           Router               Email Monitor
     (era_poster)         (router.py)          (email_monitor)
            |                   |                   |
     Pre-Billing           Handlers             Payment Tracker
     (pre_billing)        (handlers.py)        (payment_tracker)
            |                   |                   |
     Billing              MCO Portals           Bank Portals
     (billing_web)       (auth_checker)        (bank_portals)
            |                   |
     Lauris EMR           Fax/Refax
     (lauris/billing)    (fax_refax)
                                |
                         ClickUp Tasks
                        (clickup_tasks)
                                |
                         ClickUp Poller
                        (clickup_poller)
                                |
                    +----------+----------+
                    |                     |
              Gap Reports          Self-Learning
             (gap_report)        (self_learning)
```

**Data Stores:**
- SQLite: `data/claims_history.db` (claim_history, gap_report, pre_billing_log, clickup_patient_tasks, new_denial_patterns)
- SQLite: `data/bank_reconciliation.db` (payments, email_commands)
- SQLite: `data/self_learning_proposals.db` (proposals)
- JSON: `data/era_status.json` (processed ERA IDs)
- JSON: `data/last_responseid.txt` (incremental API fetching marker)
- Files: `sessions/*.json` (browser session cookies, per-portal, per-day)
- Files: `logs/screenshots/*.png` (error screenshots)

**External Systems:**
- Claim.MD REST API (claims, ERAs, eligibility, notes, appeals)
- Lauris Online (web portal — billing, ERA posting, fax history, client records)
- MCO Portals (Sentara, United/UHC, Availity for Molina/Anthem/Aetna, Kepro/DMAS)
- Nextiva Fax (send faxes to MCOs)
- ClickUp API v2 (task management, comments, daily summaries)
- Gmail SMTP (self-learning reports, approval workflow)
- Bank Portals (Wells Fargo, Southern Bank, Bank of America)
- Dropbox (auth document storage)
