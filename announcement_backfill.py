from __future__ import annotations

import os
import sys
import time
from typing import Set

import announcement_processor as announcements
import runner
import sync_repair as repair


MAX_MESSAGES = int(os.environ.get("ANNOUNCEMENT_BACKFILL_MAX_MESSAGES", "2000"))
AI_CALL_DELAY_SECONDS = float(os.environ.get("ANNOUNCEMENT_AI_DELAY_SECONDS", "4.0"))


def main() -> int:
    if not runner.EDUSECURE_USERNAME or not runner.EDUSECURE_PASSWORD:
        print("Missing EduSecure credentials.")
        return 2

    id_token = runner.firebase_sign_in()
    if not id_token:
        print("Firebase admin sign-in failed; historical backfill will not run.")
        return 2

    print("=== HISTORICAL EDUSecure ANNOUNCEMENT AI REPAIR + BACKFILL ===")
    print("Read-free mode: no announcements collection listing before repair.")
    print("Each EduSecure message is upserted by its deterministic sourceMessageId.")
    print("Rule: messages WITH attachments stay in PDF flow; messages WITHOUT attachments may become announcements.")
    driver = runner.legacy.make_driver()
    processed: Set[str] = set()
    bottom_confirmations = 0
    reached_bottom = False

    scanned = 0
    opened = 0
    attachment_messages = 0
    upserted = 0
    duplicates = 0
    ignored = 0
    ai_retries = 0
    failures = 0

    try:
        driver.get(runner.START_URL)
        if not runner.legacy.auto_login_edusecure(driver):
            print("EduSecure login failed during announcement backfill.")
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
                print(f"Dashboard backfill scroll: {scroll_result}")
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
                + f": {message_text[:160]}"
            )

            if not runner.legacy.real_click_message_v29(driver, message):
                failures += 1
                print("Could not open historical message; continuing.")
                runner.legacy.restore_dashboard_scroll_position(driver, saved_position)
                continue

            opened += 1
            detail_text = runner.legacy.app_current_text(driver)
            attachment_url = repair.extract_attachment_url(driver, app_handle)

            driver.switch_to.window(app_handle)
            runner.legacy.restore_app_after_pdf(driver, app_handle)

            if attachment_url:
                attachment_messages += 1
                print("Attachment present -> PDF route; not creating Announcement.")
                runner.legacy.return_dashboard_and_restore_v25(
                    driver,
                    app_handle,
                    saved_position,
                )
                continue

            source_id = announcements.stable_message_id(message_text, msg_date)

            item = announcements.build_announcement(
                message_text=message_text,
                detail_text=detail_text,
                message_date=msg_date,
            )

            if not item:
                status = "ignored"
            elif item.get("_retry"):
                status = "retry"
            else:
                item["sourceMessageId"] = source_id
                document_name = announcements.announcement_document_name(source_id)
                ok = announcements.upload_announcement(
                    item,
                    msg_date,
                    id_token,
                    existing_document_name=document_name,
                )
                status = "upserted" if ok else "failed"

            if status in {"upserted", "retry"}:
                time.sleep(AI_CALL_DELAY_SECONDS)

            if status == "upserted":
                upserted += 1
            elif status == "ignored":
                ignored += 1
            elif status == "retry":
                ai_retries += 1
                print("AI metadata unavailable; this message will be retried in the next backfill run.")
            else:
                failures += 1

            runner.legacy.return_dashboard_and_restore_v25(
                driver,
                app_handle,
                saved_position,
            )

        print("\n=== ANNOUNCEMENT BACKFILL SUMMARY ===")
        print(f"Messages scanned: {scanned}")
        print(f"Messages opened: {opened}")
        print(f"Attachment/PDF messages skipped: {attachment_messages}")
        print(f"Announcements AI-upserted/repaired: {upserted}")
        print(f"Announcement duplicates skipped: {duplicates}")
        print(f"Useless messages ignored: {ignored}")
        print(f"AI retries postponed: {ai_retries}")
        print(f"Failures: {failures}")
        print(f"Reached EduSecure history bottom: {reached_bottom}")

        if reached_bottom and failures == 0 and ai_retries == 0:
            print("Historical announcement AI repair/backfill complete ✅")
            return 0

        if not reached_bottom:
            print(
                "Backfill did not reach the bottom of EduSecure history. "
                "It is intentionally NOT marked complete."
            )
        if ai_retries:
            print("AI retries remain pending, so backfill is NOT marked complete.")
        return 1

    finally:
        try:
            driver.quit()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
