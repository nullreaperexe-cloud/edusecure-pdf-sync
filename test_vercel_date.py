import unittest
from unittest.mock import patch

import sync_repair as repair


class FakeElement:
    def __init__(self, visible=True):
        self.visible = visible
        self.value = ""
        self.clicked = False

    def is_displayed(self):
        return self.visible

    def is_enabled(self):
        return True

    def click(self):
        self.clicked = True

    def send_keys(self, *args):
        for item in args:
            if str(item).startswith("Reader"):
                self.value = str(item)

    def get_attribute(self, name):
        return self.value if name == "value" else ""


class FakeVercelDriver:
    def __init__(self, onboarding=False, permission=False):
        self.url = ""
        self.onboarding = onboarding
        self.permission = permission
        self.name = FakeElement(onboarding)
        self.form_button = FakeElement(onboarding)
        self.name_modal = FakeElement(onboarding)
        self.permission_modal = FakeElement(False)
        self.skip = FakeElement(False)
        self.quit_called = False

    def get(self, url):
        self.url = url

    def find_elements(self, how, selector):
        if selector == "#nameForm #nameInput":
            return [self.name]
        if selector == "#nameModal":
            return [self.name_modal]
        if selector == "#pdfGrid, #materialsSection":
            return [FakeElement()]
        if selector.startswith("#nameForm button"):
            self.form_button.clicked = True
            self.name_modal.visible = False
            if self.permission:
                self.permission_modal.visible = True
            return [self.form_button]
        if selector == "#permissionModal":
            return [self.permission_modal]
        if selector == "#notificationLater":
            return [self.skip]
        if selector.startswith("#pdfMore"):
            return []
        return []

    def execute_script(self, script, *args):
        if args and args[0] == self.skip:
            self.skip.clicked = True
            self.permission_modal.visible = False
            return None
        return "Explore PDFs\nAdded Oct 1, 2026\nAdded Sep 30, 2026"

    def quit(self):
        self.quit_called = True


class WebsiteDateTests(unittest.TestCase):
    def test_uses_vercel_not_discontinued_domain(self):
        self.assertEqual(repair.WEBSITE_URL, "https://8apdf.vercel.app/")

    def test_card_dates_not_circular_dates(self):
        text = "Circular dated October 2, 2026\nAdded Oct 1, 2026\nAdded Sep 30, 2026"
        dates = repair.parse_website_added_dates(text)
        self.assertEqual(max(dates).isoformat(), "2026-10-01")

    @patch("sync_repair.time.sleep")
    @patch("sync_repair.runner.legacy.wait_ready")
    @patch("sync_repair.runner.legacy.make_driver")
    def test_live_reader_opens_vercel(self, create_driver, _ready, _sleep):
        driver = FakeVercelDriver(onboarding=False)
        create_driver.return_value = driver
        value = repair.read_latest_date_from_website()
        self.assertEqual(driver.url, "https://8apdf.vercel.app/")
        self.assertEqual(value.isoformat(), "2026-10-01")
        self.assertTrue(driver.quit_called)

    @patch("sync_repair.time.sleep")
    def test_onboarding_enters_random_name_and_skips_notifications(self, _sleep):
        driver = FakeVercelDriver(onboarding=True, permission=True)
        self.assertTrue(repair.complete_vercel_onboarding(driver))
        self.assertTrue(driver.name.value.startswith("Reader"))
        self.assertTrue(driver.form_button.clicked)
        self.assertTrue(driver.skip.clicked)


if __name__ == "__main__":
    unittest.main()
