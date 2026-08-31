from __future__ import annotations

import re
import sys
import time
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import runner
import runner_v4


WEBSITE_URL = "https://eightapdf-study-library.nullreaper-exe.chatgpt.site/?i=1"
ORIGINAL_EXISTING_STATE = runner_v4.ORIGINAL_EXISTING_STATE
WEBSITE_LATEST_DATE: Optional[date] = None


class _WebsiteBoundaryDate(date):
    """Internally latest+1, while logs still show the actual website latest date."""

    def __new__(cls, internal_date: date, displayed_date: date):
        obj = date.__new__(cls, internal_date.year, internal_date.month, internal_date.day)
        obj._displayed_date = displayed_date
        return obj

    def isoformat(self) -> str:
        return self._displayed_date.isoformat()


def clean(value: Any) -> str:
    return runner.clean(value)


def parse_website_added_dates(text: str) -> List[date]:
    """Read the dates printed on study-material cards, e.g. ADDED AUG 31, 2026."""
    raw = clean(text)
    found: List[date] = []

    pattern = re.compile(
        r"\bADDED\s+([A-Z][A-Za-z]{2,8})\s+(\d{1,2}),\s+(\d{4})\b",
        re.I,
    )

    for month, day, year in pattern.findall(raw):
        parsed = None
        for fmt in ("%b %d %Y", "%B %d %Y"):
            try:
                parsed = datetime.strptime(f"{month} {day} {year}", fmt).date()
                break
            except ValueError:
                pass
        if parsed:
            found.append(parsed)

    return found


def read_latest_date_from_website() -> Optional[date]:
    """FIRST action of the automation: open 8aPDF and read its latest displayed card date."""
    driver = runner.legacy.make_driver()
    try:
        print("=== STEP 0: CHECKING 8aPDF WEBSITE LATEST PDF DATE ===")
        print(f"Opening website first: {WEBSITE_URL}")
        driver.get(WEBSITE_URL)
        runner.legacy.wait_ready(driver)
        time.sleep(1.8)

        body_text = ""
        try:
            body_text = driver.execute_script(
                "return (document.body && document.body.innerText) || '';"
            ) or ""
        except Exception:
            pass

        dates = parse_website_added_dates(body_text)
        if dates:
            latest = max(dates)
            print(f"Latest PDF date found on website: {latest.isoformat()}")
            print(
                "STRICT RULE LOCKED: only EduSecure attachments with a message date "
                f"AFTER {latest.isoformat()} can be uploaded. Same-date and older are skipped."
            )
            return latest

        # Fallback to the legacy website-date reader if card text changes.
        try:
            latest = runner.legacy.latest_library_date(driver)
        except Exception:
            latest = None

        if latest:
            print(f"Latest PDF date found by fallback reader: {latest.isoformat()}")
            print(
                "STRICT RULE LOCKED: only EduSecure attachments with a message date "
                f"AFTER {latest.isoformat()} can be uploaded. Same-date and older are skipped."
            )
            return latest

        print("ERROR: Could not read the latest PDF date from the 8aPDF website.")
        return None
    finally:
        try:
            driver.quit()
        except Exception:
            pass


def existing_state_from_website(materials: List[Dict[str, Any]]):
    """Use Firestore only for duplicate URLs/order; use WEBSITE date for the cutoff."""
    urls, _firestore_latest, next_order = ORIGINAL_EXISTING_STATE(materials)

    if WEBSITE_LATEST_DATE is None:
        raise RuntimeError("Website latest PDF date was not established before EduSecure sync")

    # runner.py treats messages with msg_date < cutoff as old when latest_date is truthy.
    # Supplying website_latest + 1 therefore makes the boundary STRICTLY AFTER website_latest.
    effective = _WebsiteBoundaryDate(
        WEBSITE_LATEST_DATE + timedelta(days=1),
        WEBSITE_LATEST_DATE,
    )

    print(f"Website cutoff date being used: {WEBSITE_LATEST_DATE.isoformat()}")
    print("Same-date and older EduSecure messages are NOT eligible for upload.")
    return urls, effective, next_order


def main() -> int:
    global WEBSITE_LATEST_DATE

    # This must happen before Firebase/Firestore and before EduSecure.
    WEBSITE_LATEST_DATE = read_latest_date_from_website()
    if WEBSITE_LATEST_DATE is None:
        print("Stopping safely: website latest date could not be read, so no EduSecure upload will run.")
        return 2

    # runner_v4 import already installs the exact Attachment/new-tab extractor.
    # Override only the date boundary so it comes from the public 8aPDF website.
    runner.existing_state = existing_state_from_website

    return runner.main()


if __name__ == "__main__":
    sys.exit(main())
