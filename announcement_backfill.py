from __future__ import annotations

import os
import sys
import time
from typing import Any, Dict, List, Set

import announcement_processor as announcements
import openrouter_title as ai_title
import runner
import sync_repair as repair


MAX_MESSAGES = int(os.environ.get("ANNOUNCEMENT_BACKFILL_MAX_MESSAGES", "2000"))
AI_BATCH_SIZE = int(os.environ.get("ANNOUNCEMENT_AI_BATCH_SIZE", "15"))


def _ai_text(message_text: str, detail_text: str) -> str:
    primary = announcements.prepare_announcement_ai_text(message_text)
    detail = announcements.prepare_announcement_ai_text(detail_text)

    if primary and len(primary) >= 40:
        return primary[:2500]
    if primary and detail and detail != primary:
        return (primary + "\n" + detail)[:2500]
    return (primary or detail)[:2500]


def main() -> int:
    if not runner.EDUSECURE_USERNAME or not runner.EDUSECURE_PASSWORD:
        print("Missing EduSecure credentials.")
        return 2

    id_token = runner.firebase_sign_in()
    if not id_token:
        print("Firebase admin sign-in failed; historical repair will not run.")
        return 2

    print("=== HISTORICAL EDUSecure ANNOUNCEMENT BATCH AI REPAIR ===")
    print("No announcements collection listing.")
    print("Dates/order are repaired directly from EduSecure message dates.")
    print(f"AI batch size: {AI_BATCH_SIZE}")
    print("Messages WITH attachments remain PDF-only.")

    driver = runner.legacy.make_driver()
    processed: Set[str] = set()
    bottom_confirmations = 0
    reached_bottom = False

    scanned = 0
    opened = 0
    attachment_messages = 0
    ignored = 0
    failures = 0
    date_repairs = 0

    pending: List[Dict[str, Any]] = []

    try:
        driver.get(runner.START_URL)
        if not runner.legacy.auto_login_edusecure(driver):
            print("EduSecure login failed during announcement repair.")
            runner.legacy.save_debug_screenshot(driver, "debug_announcement_backfill_login.png")
            return 2

        driver.get(runner.START_URL)
        runner.legacy.wait_ready(driver)
        time.sleep(0.9)
        app_handle = driver.current_window_handle
        runner.legacy.restore_dashboard_scroll_position(driver, 0)

        while scanned < MAX_MESSAGES:
            driver.switch_to.window(app_handle)
            runner.legacy.restore_app_after_pdf(driver, app_handle)

            if "dashboard.aspx" not in driver.current_url.lower():
                driver.get(runner.START_URL)
                runner.legacy.wait_ready(driver)
                time.sleep(0.65)

            visible = runner.legacy.find_visible_dashboard_messages_v29(driver, processed)
            if not visible:
                scroll_result = runner.legacy.dashboard_scroll_v24(driver)
                print(f"Dashboard repair scroll: {scroll_result}")
                if scroll_result.get("atBottom"):
                    bottom_confirmations += 1
                else:
                    bottom_confirmations = 0

                if bottom_confirmations >= 5:
                    reached_bottom = True
                    break
                continue

            message = visible[0]
            message_text = runner.clean(message.get("text"))
            fp = message.get("fp") or runner.legacy.fingerprint(message_text)
            if fp:
                processed.add(fp)

            scanned += 1
            msg_date = runner.legacy.extract_message_date(message_text)
            saved_position = runner.legacy.get_dashboard_scroll_position(driver)

            print(
                f"[{scanned}] Opening historical message"
                + (f" dated {msg_date.isoformat()}" if msg_date else "")
                + f": {message_text[:140]}"
            )

            if not runner.legacy.real_click_message_v29(driver, message):
                failures += 1
                runner.legacy.restore_dashboard_scroll_position(driver, saved_position)
                continue

            opened += 1
            detail_text = runner.legacy.app_current_text(driver)
            attachment_url = repair.extract_attachment_url(driver, app_handle)

            driver.switch_to.window(app_handle)
            runner.legacy.restore_app_after_pdf(driver, app_handle)

            if attachment_url:
                attachment_messages += 1
                runner.legacy.return_dashboard_and_restore_v25(
                    driver,
                    app_handle,
                    saved_position,
                )
                continue

            if not announcements.useful_announcement(message_text):
                ignored += 1
                runner.legacy.return_dashboard_and_restore_v25(
                    driver,
                    app_handle,
                    saved_position,
                )
                continue

            source_id = announcements.stable_message_id(message_text, msg_date)

            # Fix ordering NOW, independently of OpenRouter quota.
            if msg_date and announcements.patch_announcement_dates(
                source_id,
                msg_date,
                id_token,
            ):
                date_repairs += 1

            ai_text = _ai_text(message_text, detail_text)
            if not ai_text:
                ignored += 1
            else:
                pending.append(
                    {
                        "id": source_id,
                        "text": ai_text,
                        "message_text": message_text,
                        "message_date": msg_date,
                    }
                )

            runner.legacy.return_dashboard_and_restore_v25(
                driver,
                app_handle,
                saved_position,
            )

        print(
            f"Historical scan complete: {len(pending)} announcements queued for batch AI."
        )

        repaired = 0
        ai_retries = 0

        for offset in range(0, len(pending), AI_BATCH_SIZE):
            batch = pending[offset:offset + AI_BATCH_SIZE]
            request_items = [
                {"id": item["id"], "text": item["text"]}
                for item in batch
            ]

            print(
                f"OpenRouter batch {offset // AI_BATCH_SIZE + 1}: "
                f"{len(batch)} announcements"
            )

            results = ai_title.generate_announcement_batch(
                request_items,
                announcements.ALLOWED_CATEGORIES,
            )

            if results is None:
                # Stop immediately on quota/API failure. Do NOT burn more free requests.
                ai_retries += len(pending) - offset
                print(
                    "Batch AI unavailable/quota-limited. "
                    "Stopping historical AI calls immediately."
                )
                break

            for item in batch:
                meta = results.get(item["id"])
                if not meta:
                    ai_retries += 1
                    continue

                announcement_item = {
                    "title": meta["title"],
                    "description": announcements.clean_description(item["message_text"])
                    or meta["title"],
                    "category": meta["category"],
                    "subject": meta["subject"],
                    "priority": meta["priority"],
                    "aiModel": meta.get("aiModel", ""),
                    "originalMessage": item["message_text"],
                    "sourceMessageId": item["id"],
                }

                ok = announcements.upload_announcement(
                    announcement_item,
                    item["message_date"],
                    id_token,
                    existing_document_name=announcements.announcement_document_name(
                        item["id"]
                    ),
                )
                if ok:
                    repaired += 1
                else:
                    failures += 1

            # Keep well below free-model RPM even when multiple batches are needed.
            if offset + AI_BATCH_SIZE < len(pending):
                time.sleep(4)

        print("\n=== ANNOUNCEMENT BATCH REPAIR SUMMARY ===")
        print(f"Messages scanned: {scanned}")
        print(f"Messages opened: {opened}")
        print(f"Attachment/PDF messages skipped: {attachment_messages}")
        print(f"Date/order repairs applied: {date_repairs}")
        print(f"Announcements queued for AI: {len(pending)}")
        print(f"Announcements AI-repaired: {repaired}")
        print(f"AI retries pending: {ai_retries}")
        print(f"Useless messages ignored: {ignored}")
        print(f"Failures: {failures}")
        print(f"Reached EduSecure history bottom: {reached_bottom}")

        if reached_bottom and failures == 0 and ai_retries == 0:
            print("Historical announcement batch AI repair complete ✅")
            return 0

        return 1

    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
