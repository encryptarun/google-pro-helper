import asyncio
import datetime
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ENGINE_PATH = Path(__file__).resolve().parents[1] / "automation_engine.py"


def load_engine():
    spec = importlib.util.spec_from_file_location("checkout_verification_engine", ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load automation engine from {ENGINE_PATH}")
    engine = importlib.util.module_from_spec(spec)
    playwright = types.ModuleType("playwright")
    async_api = types.ModuleType("playwright.async_api")
    async_api.Error = type("PlaywrightError", (Exception,), {})
    async_api.async_playwright = mock.Mock()
    with (
        mock.patch.object(os.path, "exists", return_value=True),
        mock.patch.object(os, "makedirs"),
        mock.patch.dict(
            sys.modules, {"playwright": playwright, "playwright.async_api": async_api}
        ),
    ):
        spec.loader.exec_module(engine)
    return engine


class CheckoutVerificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = load_engine()
        self.automator = self.engine.GoogleUltraAutomator(
            {"id": "test", "email": "test@example.invalid"}, {}, on_log=lambda _: None
        )

    def test_checkout_amount_due_now(self):
        check = self.engine._checkout_due_now_is_zero
        for text in (
            "Google AI Pro | Total today US$10.00 | Free trial",
            "Google AI Pro | Total today US$20.00 | Discount US$0.00",
            "Google AI Pro | Free trial | Renewal US$10.00",
            "Google AI Pro | US$0.00 | Renewal US$10.00",
            "Google AI Pro | Due today US$0.00 | Service fee US$2.00",
            "Google AI Pro | Due today US$0.00 | Service fee US$2.00 then renews US$19.99",
            "Google AI Pro | Due today US$0.00 | Amount due today US$10.00",
            "Google AI Pro | Due today | US$19.99 after trial",
        ):
            with self.subTest(text=text):
                self.assertFalse(check(text))
        self.assertTrue(
            check("Google AI Pro | Due today US$0.00 | Renews at US$19.99 after trial")
        )
        self.assertTrue(check("Google AI Pro | Due today US$0.00 | US$19.99/month after trial"))
        self.assertTrue(check("Google AI Pro | Due today | US$0.00 | Renews at US$19.99"))

    async def test_paid_checkout_stops_before_subscribe_click(self):
        page = mock.Mock(url="https://one.google.com/plans")
        page.evaluate = mock.AsyncMock(return_value={"text": "Get started"})
        self.automator.safe_goto = mock.AsyncMock()
        self.automator.save_screenshot = mock.AsyncMock()
        frame = mock.Mock()
        self.automator._poll_eacquire_modal = mock.AsyncMock(
            return_value=(frame, "Due today US$10.00 | Free trial | US$0.00 discount")
        )
        with mock.patch.object(asyncio, "sleep", new_callable=mock.AsyncMock):
            result = await self.automator.open_and_click_subscribe(page, {}, "layer1")
        self.assertFalse(result)
        self.assertIn("Zero-cost protection", self.automator.last_subscribe_error)
        frame.locator.assert_not_called()

    def make_page(self, initial_text, final_text):
        locator = mock.Mock()
        locator.count = mock.AsyncMock(return_value=0)
        locator.is_visible = mock.AsyncMock(return_value=False)
        locator.first = locator
        locator.last = locator
        locator.nth.return_value = locator
        page = mock.Mock()
        page.inner_text = mock.AsyncMock(side_effect=[initial_text, final_text])
        page.get_by_text.return_value = locator
        page.locator.return_value = locator
        page.evaluate = mock.AsyncMock()
        page.context.pages = [page]
        self.automator.safe_goto = mock.AsyncMock()
        self.automator.save_screenshot = mock.AsyncMock(return_value="shot.png")
        return page

    async def run_final(self, page):
        with mock.patch.object(asyncio, "sleep", new_callable=mock.AsyncMock):
            await self.automator.step5_cancel_and_verify(page)

    async def test_empty_subscription_page_cannot_complete(self):
        page = self.make_page("No subscriptions", "No subscriptions")
        with self.assertRaisesRegex(RuntimeError, "identify one Google AI subscription"):
            await self.run_final(page)
        self.assertNotEqual(self.automator.account["status"], "completed")

    async def test_failed_cancellation_cannot_complete(self):
        date = datetime.date.today() + datetime.timedelta(days=700)
        date_text = date.isoformat()
        final = f"Google AI Pro (5 TB) | Renewal date: {date_text} | Manage"
        page = self.make_page(final, final)
        with self.assertRaisesRegex(RuntimeError, "renewal was cancelled"):
            await self.run_final(page)
        self.assertNotEqual(self.automator.account["status"], "completed")

    async def test_missing_expiration_date_cannot_complete(self):
        final = "Google AI Pro (5 TB) | Cancelled | Manage"
        page = self.make_page(final, final)
        with self.assertRaisesRegex(RuntimeError, "expiration date"):
            await self.run_final(page)
        self.assertNotEqual(self.automator.account["status"], "completed")

    def test_us_numeric_source_date_is_not_read_as_day_first(self):
        parsed = self.engine._parse_english_date("10/08/2028")
        self.assertEqual(parsed, datetime.date(2028, 10, 8))

    async def test_english_expiry_formats_are_stored_as_iso_dates(self):
        date = datetime.date.today() + datetime.timedelta(days=700)
        formats = (
            f"{date.day} {date.strftime('%B')} {date.year}",
            f"{date.strftime('%b')} {date.day}, {date.year}",
            date.strftime("%m/%d/%Y"),
            date.isoformat(),
        )
        for value in formats:
            with self.subTest(value=value):
                self.setUp()
                final = f"Google AI Pro (5 TB) | Cancelled | Expires on {value}"
                await self.run_final(self.make_page(final, final))
                self.assertEqual(self.automator.account["expiry_date"], date.isoformat())

    async def test_invalid_or_ambiguous_expiry_cannot_complete(self):
        date = datetime.date.today() + datetime.timedelta(days=700)
        other = date + datetime.timedelta(days=1)
        cases = (
            "Google AI Pro (5 TB) | Cancelled | Expires on 32/13/2028",
            f"Google AI Pro (5 TB) | Cancelled | Expires on {date:%m/%d/%Y} | Renews on {other:%m/%d/%Y}",
            "Google AI Pro (5 TB) | Cancelled | Expires on 01/01/2020",
        )
        for final in cases:
            with self.subTest(final=final):
                self.setUp()
                with self.assertRaises(RuntimeError):
                    await self.run_final(self.make_page(final, final))
                self.assertNotEqual(self.automator.account["status"], "completed")

    async def test_verified_subscription_reports_only_supported_claims(self):
        date = datetime.date.today() + datetime.timedelta(days=700)
        date_text = date.isoformat()
        final = f"Google AI Pro (5 TB) | Cancelled | Expires on {date_text}"
        page = self.make_page(final, final)
        await self.run_final(page)
        self.assertEqual(self.automator.account["status"], "completed")
        self.assertEqual(self.automator.account["expiry_date"], date_text)
        self.assertIn("Renewal cancelled", self.automator.account["step_message"])
        self.assertNotIn("Two-year", self.automator.account["step_message"])


if __name__ == "__main__":
    unittest.main()
