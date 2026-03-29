"""
lauris/diagnosis.py
-------------------
Extract diagnosis from Mental Health Assessment 3.0 in Lauris
and update the Client Face Sheet with the diagnosis code.

Flow:
  1. Navigate to consumer's Documents tab
  2. Find and open "Mental Health Assessment 3.0"
  3. Iterate through SSRS ReportViewer pages looking for "Diagnostic Impression"
  4. Extract ICD-10 code from the line after the heading
  5. Optionally update the Client Face Sheet with the extracted code

NOTE: The consumer search uses autocomplete — type 3 chars, wait, click
dropdown item. The Documents tab is inside iframe `map-iframesec`.
The assessment opens as a separate page (not inside the iframe).
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

from playwright.async_api import Page, TimeoutError as PWTimeout

from config.models import Claim
from config.settings import DRY_RUN
from logging_utils.logger import get_logger

logger = get_logger("lauris_diagnosis")

# Regex to extract ICD-10 code and description from diagnostic impression line
# Matches patterns like "F25.1 - Schizoaffective Disorder, depressed type"
ICD10_PATTERN = re.compile(r"(F\d{2}\.\d+)\s*-?\s*(.*)")


# ---------------------------------------------------------------------------
# Consumer navigation helpers
# ---------------------------------------------------------------------------

async def _navigate_to_consumer(page: Page, consumer_uid: str) -> bool:
    """
    Navigate to a consumer's profile in Lauris using UID search.

    The consumer UID format is "ID######" (e.g., "ID004665").
    Uses the start_newui.aspx autocomplete search.

    Returns True if the consumer profile loaded successfully.
    """
    base = page.url.split("/start_newui")[0] if "start_newui" in page.url else page.url.rsplit("/", 1)[0]

    # Navigate to consumers page
    await page.goto(
        f"{base}/start_newui.aspx",
        wait_until="domcontentloaded",
        timeout=20000,
    )
    await asyncio.sleep(2)

    # Type first 3 characters of UID into search (triggers autocomplete)
    search_input = await page.query_selector("#txtSearch")
    if not search_input:
        logger.error("Consumer search field #txtSearch not found")
        return False

    # Clear and type — autocomplete triggers on 3+ chars
    await search_input.fill("")
    await search_input.type(consumer_uid[:3], delay=100)
    await asyncio.sleep(2)  # Wait for autocomplete dropdown

    # If UID is longer, type the rest to narrow results
    if len(consumer_uid) > 3:
        await search_input.type(consumer_uid[3:], delay=50)
        await asyncio.sleep(1)

    # Click the matching autocomplete item
    autocomplete_item = await page.query_selector(
        f"div.autocomplete_item:has-text('{consumer_uid}')"
    )
    if not autocomplete_item:
        # Try broader match
        autocomplete_items = await page.query_selector_all("div.autocomplete_item")
        for item in autocomplete_items:
            text = await item.inner_text()
            if consumer_uid in text:
                autocomplete_item = item
                break

    if not autocomplete_item:
        # Not found in Active Consumers — check Discharged Consumers
        logger.info(
            "Consumer not found in Active — checking Discharged Consumers",
            uid=consumer_uid,
        )
        discharged_link = await page.query_selector(
            "a:has-text('Discharged Consumers'), "
            "#ctl00_ContentPlaceHolder1_LinkButton2"
        )
        if discharged_link:
            await discharged_link.click()
            await asyncio.sleep(3)

            # Search again in discharged list
            search_input = await page.query_selector("#txtSearch")
            if search_input:
                await search_input.fill("")
                await search_input.type(consumer_uid[:3], delay=100)
                await asyncio.sleep(2)
                if len(consumer_uid) > 3:
                    await search_input.type(consumer_uid[3:], delay=50)
                    await asyncio.sleep(1)

                autocomplete_item = await page.query_selector(
                    f"div.autocomplete_item:has-text('{consumer_uid}')"
                )
                if not autocomplete_item:
                    autocomplete_items = await page.query_selector_all(
                        "div.autocomplete_item"
                    )
                    for item in autocomplete_items:
                        text = await item.inner_text()
                        if consumer_uid in text:
                            autocomplete_item = item
                            break

        if not autocomplete_item:
            logger.error(
                "Consumer not found in Active or Discharged",
                uid=consumer_uid,
            )
            return False

    await autocomplete_item.click()
    await asyncio.sleep(2)

    # Click the consumer's row link in the GridView grid
    grid_link = await page.query_selector(
        f"a[href*='keyval']:has-text('{consumer_uid}'), "
        f"tr:has-text('{consumer_uid}') a[href*='dataobject']"
    )
    if not grid_link:
        # Try any row in the grid that contains the UID
        rows = await page.query_selector_all("table tr")
        for row in rows:
            row_text = await row.inner_text()
            if consumer_uid in row_text:
                # Check for onclick link (used in Discharged grid)
                link = await row.query_selector("a[onclick]")
                if link:
                    grid_link = link
                    break
                # Check for href link (used in Active grid)
                link = await row.query_selector("a[href]")
                if link:
                    grid_link = link
                    break

    if not grid_link:
        # Last resort: call LoadConsumerDetails directly via JS
        loaded = await page.evaluate(
            f"() => {{ if (typeof LoadConsumerDetails === 'function') "
            f"{{ LoadConsumerDetails('{consumer_uid}'); return true; }} "
            f"return false; }}"
        )
        if loaded:
            await asyncio.sleep(3)
            logger.info("Loaded consumer via JS", uid=consumer_uid)
            return True

        logger.error("Consumer row link not found in grid", uid=consumer_uid)
        return False

    await grid_link.click()
    await asyncio.sleep(3)

    # Wait for the consumer profile iframe to load
    for _ in range(10):
        iframe = page.frame("map-iframesec")
        if iframe and "keyval" in (iframe.url or ""):
            break
        await asyncio.sleep(1)

    logger.info("Navigated to consumer profile", uid=consumer_uid)
    return True


async def _navigate_to_documents(page: Page) -> Page:
    """
    Click the Documents tab inside the consumer profile iframe.
    Clears the start date filter and clicks Search to show all docs.

    Returns the iframe page object for further interaction.
    """
    # Consumer profile loads in iframe named "map-iframesec"
    # Try frame object first (more reliable than querySelector)
    iframe = page.frame("map-iframesec")
    if not iframe:
        # Fallback: find iframe by src containing dataobject/keyval
        iframe_el = await page.query_selector(
            "iframe[name='map-iframesec'], "
            "iframe[src*='dataobject'], "
            "iframe[src*='keyval']"
        )
        if iframe_el:
            iframe = await iframe_el.content_frame()

    if not iframe:
        logger.error("Consumer profile iframe not found")
        raise RuntimeError("Consumer profile iframe not found")

    # Click the Documents tab
    docs_tab = await iframe.query_selector("a:has-text('Documents')")
    if not docs_tab:
        # Try alternative selectors
        docs_tab = await iframe.query_selector(
            "a[href*='Documents'], a[href*='documents'], "
            "li:has-text('Documents') a, td:has-text('Documents') a"
        )

    if not docs_tab:
        raise RuntimeError("Documents tab not found in consumer profile")

    await docs_tab.click()
    await asyncio.sleep(2)

    # Clear the start date filter to show ALL documents
    await iframe.evaluate("""
        () => {
            const txtStart = document.getElementById('txtStart');
            if (txtStart) txtStart.value = '';
            const btnSearch = document.getElementById('btnSearch');
            if (btnSearch) btnSearch.click();
        }
    """)
    await asyncio.sleep(5)  # Wait for document grid to reload

    return iframe


async def _find_document_link(iframe: Page, doc_name: str) -> str | None:
    """
    Find a document by name in the DocsGrid1 table and return its
    enterformdata link URL.
    """
    rows = await iframe.query_selector_all("#DocsGrid1 tr")
    for row in rows:
        row_text = await row.inner_text()
        if doc_name.lower() in row_text.lower():
            link = await row.query_selector("a[href*='enterformdata']")
            if link:
                href = await link.get_attribute("href")
                return href
            # Also check for onclick or other link patterns
            link = await row.query_selector("a[href]")
            if link:
                href = await link.get_attribute("href")
                if href:
                    return href
    return None


# ---------------------------------------------------------------------------
# Diagnosis extraction from Mental Health Assessment 3.0
# ---------------------------------------------------------------------------

async def extract_diagnosis_from_assessment(
    page: Page,
    consumer_uid: str,
) -> dict | None:
    """
    Extract the ICD-10 diagnosis from the Mental Health Assessment 3.0
    in Lauris for a given consumer.

    Args:
        page: Playwright page with an active Lauris session.
        consumer_uid: Consumer UID in Lauris (e.g., "ID004665").

    Returns:
        {"icd_code": "F25.1", "description": "Schizoaffective Disorder, depressed type", "page_found": 2}
        or None if not found.
    """
    logger.info("Extracting diagnosis from assessment", uid=consumer_uid)

    # Navigate to the consumer
    if not await _navigate_to_consumer(page, consumer_uid):
        return None

    # Navigate to Documents tab
    try:
        iframe = await _navigate_to_documents(page)
    except RuntimeError as e:
        logger.error("Failed to navigate to Documents", uid=consumer_uid, error=str(e))
        return None

    # Find the Mental Health Assessment 3.0 link
    assessment_href = await _find_document_link(iframe, "Mental Health Assessment 3.0")
    if not assessment_href:
        logger.warning(
            "Mental Health Assessment 3.0 not found in documents",
            uid=consumer_uid,
        )
        return None

    # Build full URL if href is relative
    base = page.url.split("/start_newui")[0] if "start_newui" in page.url else page.url.rsplit("/", 1)[0]
    if assessment_href.startswith("/"):
        assessment_url = base.split("//")[0] + "//" + base.split("//")[1].split("/")[0] + assessment_href
    elif not assessment_href.startswith("http"):
        assessment_url = f"{base}/{assessment_href}"
    else:
        assessment_url = assessment_href

    # Open assessment in a new page (it opens outside the iframe)
    assessment_page = await page.context.new_page()
    try:
        await assessment_page.goto(assessment_url, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)  # SSRS ReportViewer needs time to load

        result = await _scan_report_for_diagnosis(assessment_page)
        return result

    except Exception as e:
        logger.error("Failed to open/scan assessment", uid=consumer_uid, error=str(e))
        return None

    finally:
        await assessment_page.close()


async def _scan_report_for_diagnosis(report_page: Page) -> dict | None:
    """
    Scan through all pages of an SSRS ReportViewer looking for
    "Diagnostic Impression" and extract the ICD-10 code.

    The report content is in iframe named "ReportViewer1_ContentFrame".
    Page navigation uses toolbar buttons.
    """
    # Get total page count
    total_pages = 1
    try:
        total_el = await report_page.query_selector(
            "#ReportToolbar1_Menu_ITCNT6_PageCount_I"
        )
        if total_el:
            total_text = await total_el.get_attribute("value")
            if not total_text:
                total_text = await total_el.inner_text()
            total_text = (total_text or "").strip()
            if total_text.isdigit():
                total_pages = int(total_text)
    except Exception:
        pass

    logger.info("Assessment report has pages", total=total_pages)

    # Navigate to page 1 first (may already be there)
    try:
        page_input = await report_page.query_selector(
            "#ReportToolbar1_Menu_ITCNT5_PageNumber_I"
        )
        if page_input:
            await page_input.fill("1")
            await report_page.keyboard.press("Enter")
            await asyncio.sleep(3)
    except Exception:
        pass

    for page_num in range(1, total_pages + 1):
        logger.info("Scanning report page", page=page_num, total=total_pages)

        # Read content from the report content iframe
        # Must re-query the frame each time — content changes after navigation
        content_frame = report_page.frame("ReportViewer1_ContentFrame")
        if content_frame:
            page_text = await content_frame.evaluate(
                "() => document.body.innerText"
            )
        else:
            content_frame_el = await report_page.query_selector(
                "iframe[name='ReportViewer1_ContentFrame']"
            )
            if content_frame_el:
                cf = await content_frame_el.content_frame()
                page_text = await cf.evaluate(
                    "() => document.body.innerText"
                ) if cf else ""
            else:
                page_text = await report_page.evaluate(
                    "() => document.body.innerText"
                )

        # Clean whitespace for matching — replace non-breaking spaces
        page_text = page_text.replace("\xa0", " ")
        page_text = "\n".join(
            l.strip() for l in page_text.split("\n") if l.strip()
        )

        # Look for "Diagnostic Impression" heading
        if "diagnostic impression" in page_text.lower():
            logger.info("Found 'Diagnostic Impression' on page", page=page_num)

            # Extract the ICD-10 code from the text after the heading
            result = _extract_icd_from_text(page_text, page_num)
            if result:
                return result

        # Navigate to next page (unless we're on the last page)
        if page_num < total_pages:
            next_btn = await report_page.query_selector(
                "#ReportToolbar1_Menu_DXI7_Img"
            )
            if next_btn:
                await next_btn.click()
                await asyncio.sleep(4)  # Wait for new page to render

    logger.warning("Diagnostic Impression not found in any page of the assessment")
    return None


def _extract_icd_from_text(page_text: str, page_num: int) -> dict | None:
    """
    Extract ICD-10 code from the text surrounding "Diagnostic Impression".

    The ICD-10 code appears on the line immediately after the heading.
    Example:
        Diagnostic Impression
        F25.1 - Schizoaffective Disorder, depressed type
    """
    lines = page_text.split("\n")
    found_heading = False

    for line in lines:
        stripped = line.strip()

        if found_heading:
            # This line should contain the ICD-10 code
            match = ICD10_PATTERN.search(stripped)
            if match:
                icd_code = match.group(1)
                description = match.group(2).strip().rstrip(".")
                logger.info(
                    "Extracted diagnosis",
                    icd_code=icd_code,
                    description=description,
                    page=page_num,
                )
                return {
                    "icd_code": icd_code,
                    "description": description,
                    "page_found": page_num,
                }

            # The heading was found but no ICD code on the next non-empty line
            # Keep scanning a few more lines in case of blank lines
            if stripped:
                # Non-empty line with no ICD match — could be a sub-heading
                # Continue checking for a few more lines
                continue

        if "diagnostic impression" in stripped.lower():
            found_heading = True

    # Second pass: look for any F-code anywhere near "Diagnostic Impression"
    diag_section = False
    for line in lines:
        stripped = line.strip()
        if "diagnostic impression" in stripped.lower():
            diag_section = True
            continue
        if diag_section:
            match = ICD10_PATTERN.search(stripped)
            if match:
                icd_code = match.group(1)
                description = match.group(2).strip().rstrip(".")
                logger.info(
                    "Extracted diagnosis (second pass)",
                    icd_code=icd_code,
                    description=description,
                    page=page_num,
                )
                return {
                    "icd_code": icd_code,
                    "description": description,
                    "page_found": page_num,
                }
            # Stop scanning after 10 non-empty lines past the heading
            if stripped:
                diag_section_lines = getattr(_extract_icd_from_text, "_counter", 0) + 1
                if diag_section_lines > 10:
                    break

    logger.warning("ICD-10 code not found near 'Diagnostic Impression'")
    return None


# ---------------------------------------------------------------------------
# Client Face Sheet update
# ---------------------------------------------------------------------------

async def update_facesheet_diagnosis(
    page: Page,
    consumer_uid: str,
    icd_code: str,
    description: str,
) -> bool:
    """
    Open the Client Face Sheet for a consumer and update the diagnosis field.

    Args:
        page: Playwright page with an active Lauris session.
        consumer_uid: Consumer UID (e.g., "ID004665").
        icd_code: ICD-10 code (e.g., "F25.1").
        description: Diagnosis description (e.g., "Schizoaffective Disorder, depressed type").

    Returns True on success.
    """
    if DRY_RUN:
        logger.info(
            "DRY_RUN: Would update facesheet diagnosis",
            uid=consumer_uid,
            icd_code=icd_code,
            description=description,
        )
        return True

    logger.info(
        "Updating facesheet diagnosis",
        uid=consumer_uid,
        icd_code=icd_code,
    )

    # Navigate to the consumer (if not already there)
    if not await _navigate_to_consumer(page, consumer_uid):
        return False

    # Navigate to Documents tab
    try:
        iframe = await _navigate_to_documents(page)
    except RuntimeError as e:
        logger.error("Failed to navigate to Documents", uid=consumer_uid, error=str(e))
        return False

    # Find the Client Face Sheet "Edit Form" link (not the readonly view)
    edit_href = await iframe.evaluate("""() => {
        const rows = document.querySelectorAll('#DocsGrid1 tr');
        for (const row of rows) {
            if (row.innerText.includes('Client Face Sheet')) {
                for (const a of row.querySelectorAll('a')) {
                    if (a.innerText.includes('Edit Form')) return a.href;
                }
            }
        }
        return null;
    }""")
    if not edit_href:
        logger.error("Client Face Sheet Edit Form not found", uid=consumer_uid)
        return False

    facesheet_page = await page.context.new_page()
    try:
        await facesheet_page.goto(edit_href, wait_until="domcontentloaded", timeout=30000)
        await asyncio.sleep(5)

        diagnosis_value = f"{icd_code} - {description}"

        # Primary diagnosis is a SELECT dropdown: PolicyField46
        # Try selecting by label text containing the ICD code
        diag_select = await facesheet_page.query_selector(
            "select[name*='PolicyField46']"
        )
        filled = False

        if diag_select:
            # Get all options and find the one matching our ICD code
            options = await facesheet_page.evaluate("""(sel) => {
                const select = document.querySelector(sel);
                if (!select) return [];
                return Array.from(select.options).map(o => ({
                    value: o.value, text: o.text
                }));
            }""", "select[name*='PolicyField46']")

            # Find option containing our ICD code
            for opt in options:
                if icd_code in opt["text"] or icd_code in opt["value"]:
                    await facesheet_page.select_option(
                        "select[name*='PolicyField46']",
                        value=opt["value"],
                    )
                    filled = True
                    logger.info(
                        "Diagnosis selected in dropdown",
                        option=opt["text"][:60],
                    )
                    break

        if not filled:
            # Fallback: try label-based search for any diagnosis field
            diag_field = await facesheet_page.evaluate("""() => {
                const labels = document.querySelectorAll('label, td');
                for (const el of labels) {
                    const text = (el.innerText || '').trim().toLowerCase();
                    if (text.includes('mental health diagnosis') || text.includes('primary diag')) {
                        const parent = el.closest('tr') || el.parentElement;
                        const input = parent ? parent.querySelector('select, input, textarea') : null;
                        if (input) return {name: input.name, tag: input.tagName};
                    }
                }
                return null;
            }""")

            if diag_field:
                sel = f"{diag_field['tag'].lower()}[name='{diag_field['name']}']"
                if diag_field["tag"] == "SELECT":
                    try:
                        await facesheet_page.select_option(sel, label=diagnosis_value)
                        filled = True
                    except Exception:
                        # Try partial match
                        opts = await facesheet_page.evaluate(f"""() => {{
                            const s = document.querySelector("{sel}");
                            return s ? Array.from(s.options).map(o => ({{v: o.value, t: o.text}})) : [];
                        }}""")
                        for o in opts:
                            if icd_code in o["t"]:
                                await facesheet_page.select_option(sel, value=o["v"])
                                filled = True
                                break
                else:
                    await facesheet_page.fill(sel, diagnosis_value)
                    filled = True

        if not filled:
            logger.error("Could not set diagnosis on facesheet", uid=consumer_uid)
            return False

        # Save the form — button is "Update Form" in Lauris
        save_btn = await facesheet_page.query_selector(
            "input[value*='Update Form'], input[value*='Save'], "
            "button:has-text('Update'), button:has-text('Save'), "
            "input[id*='btnP']:not([value*='Preview'])"
        )
        if save_btn and await save_btn.is_visible():
            await save_btn.click()
            await asyncio.sleep(3)
            logger.info("Facesheet saved", uid=consumer_uid, icd_code=icd_code)
        else:
            logger.warning("Save button not found on facesheet")
            return False

        return True

    except Exception as e:
        logger.error("Facesheet update failed", uid=consumer_uid, error=str(e))
        return False

    finally:
        await facesheet_page.close()


# ---------------------------------------------------------------------------
# UID lookup from record number (Medicaid ID -> Lauris UID)
# ---------------------------------------------------------------------------

_member_to_uid_cache: dict = {}


def _load_member_uid_mapping() -> dict:
    """Load member ID -> Lauris UID mapping from auth XML (cached)."""
    global _member_to_uid_cache
    if _member_to_uid_cache:
        return _member_to_uid_cache

    try:
        import os
        import requests
        import xml.etree.ElementTree as ET

        base = "https://www12.laurisonline.com"
        user = os.getenv("LAURIS_USERNAME", "")
        pwd = os.getenv("LAURIS_PASSWORD", "")
        if not user or not pwd:
            return {}

        auth_url = (
            f"{base}/reports/formsearchdataviewXML.aspx"
            "?viewid=E1jRUaNGKAxt%2bAa7Ubk1xg%3d%3d"
        )
        r = requests.get(
            auth_url, auth=(user, pwd), timeout=120
        )
        root = ET.fromstring(r.text)

        for row in root.findall(
            ".//Authorization_Information_View"
        ):
            member = (
                row.findtext("Insurance_Policy_No") or ""
            ).strip()
            uid = (row.findtext("Key") or "").strip()
            if member and uid:
                _member_to_uid_cache[member] = uid

        logger.info(
            "Member->UID mapping loaded from auth XML",
            count=len(_member_to_uid_cache),
        )
    except Exception as e:
        logger.warning(
            "Failed to load member->UID mapping",
            error=str(e),
        )

    return _member_to_uid_cache


async def _lookup_uid_from_record_number(
    page: Page,
    record_number: str,
) -> str | None:
    """
    Look up a consumer's Lauris UID from their member/Medicaid ID.

    Uses the Lauris auth XML mapping (fast, no browser needed).
    Falls back to browser Record Number search if XML mapping
    doesn't have the member ID.

    Returns the UID string (e.g., "ID004665") or None.
    """
    # Fast path: XML mapping
    mapping = _load_member_uid_mapping()
    if record_number in mapping:
        uid = mapping[record_number]
        logger.info(
            "UID found via XML mapping",
            record_number=record_number,
            uid=uid,
        )
        return uid

    # Slow path: browser search
    base = page.url.rsplit("/", 1)[0]
    if "start_newui" not in page.url:
        await page.goto(
            f"{base}/start_newui.aspx",
            wait_until="domcontentloaded",
            timeout=20000,
        )
        await asyncio.sleep(2)

    record_field = await page.query_selector(
        "#ctl00_ContentPlaceHolder1_txtRecordNo"
    )
    if not record_field:
        logger.error("Record Number search field not found")
        return None

    await record_field.fill(record_number)

    search_btn = await page.query_selector(
        "#ctl00_ContentPlaceHolder1_btnSearch, "
        "input[value='Search'], button:has-text('Search')"
    )
    if search_btn:
        await search_btn.click()
        await asyncio.sleep(2)

    rows = await page.query_selector_all("table tr")
    for row in rows:
        row_text = await row.inner_text()
        uid_match = re.search(r"(ID\d{4,})", row_text)
        if uid_match:
            uid = uid_match.group(1)
            logger.info(
                "Found UID from browser search",
                record_number=record_number,
                uid=uid,
            )
            return uid

    logger.warning(
        "UID not found for record number",
        record_number=record_number,
    )
    return None


# ---------------------------------------------------------------------------
# High-level: get diagnosis for a claim
# ---------------------------------------------------------------------------

async def get_diagnosis_for_claim(page: Page, claim: Claim) -> dict | None:
    """
    High-level function: extract diagnosis from Lauris assessment for a claim.

    The claim's client_id is the Medicaid/record number. This function first
    looks up the Lauris UID, then extracts the diagnosis from the assessment.

    Args:
        page: Playwright page with an active Lauris session.
        claim: Claim object with client_id (Medicaid/record number).

    Returns:
        {"icd_code": "F25.1", "description": "...", "page_found": 2}
        or None if not found.
    """
    logger.info(
        "Getting diagnosis for claim",
        claim_id=claim.claim_id,
        client=claim.client_name,
        client_id=claim.client_id,
    )

    # Step 1: Look up consumer UID from record number
    consumer_uid = await _lookup_uid_from_record_number(page, claim.client_id)
    if not consumer_uid:
        logger.warning(
            "Could not find consumer UID for claim",
            claim_id=claim.claim_id,
            client_id=claim.client_id,
        )
        return None

    # Step 2: Extract diagnosis from assessment
    diagnosis = await extract_diagnosis_from_assessment(page, consumer_uid)
    if diagnosis:
        logger.info(
            "Diagnosis extracted for claim",
            claim_id=claim.claim_id,
            icd_code=diagnosis["icd_code"],
            description=diagnosis["description"],
        )
    else:
        logger.warning(
            "Could not extract diagnosis from assessment",
            claim_id=claim.claim_id,
            uid=consumer_uid,
        )

    return diagnosis
