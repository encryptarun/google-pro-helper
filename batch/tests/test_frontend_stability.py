import json
import os
import time
import unittest
from copy import deepcopy
from pathlib import Path

from playwright.sync_api import expect, sync_playwright

PAGE_PATH = Path(
    os.environ.get(
        "UI_STABILITY_PAGE",
        Path(__file__).resolve().parents[1] / "templates/index.html",
    )
)


def account(account_id, **fields):
    result = {
        "id": account_id,
        "email": f"{account_id}@example.invalid",
        "password": "test-only",
        "status": "pending",
        "current_step": 0,
    }
    result.update(fields)
    return result


class FrontendStabilityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        cls.browser = cls.playwright.chromium.launch(channel="chrome", headless=True)
        cls.html = PAGE_PATH.read_text(encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        self.page = self.context.new_page()
        self.page.set_default_timeout(5000)
        self.errors = []
        self.page.on("pageerror", lambda error: self.errors.append(str(error)))
        self.state = {
            "accounts": [account("first"), account("second")],
            "logs": ["System ready"],
            "active_count": 0,
            "card": {},
        }
        self.hold_requests = False
        self.held_routes = []
        self.page.route("**/*", self.handle_request)
        self.page.add_init_script("""window.uiTestIntervals = [];
          window.setInterval = (callback, delay) => { window.uiTestIntervals.push(delay); return 0; };""")
        self.page.goto("http://console.test/")
        self.page.wait_for_function(
            "document.querySelector('#statTotal').textContent === '2'"
        )

    def handle_request(self, route):
        if route.request.url == "http://console.test/":
            route.fulfill(content_type="text/html; charset=utf-8", body=self.html)
        elif route.request.url == "http://console.test/api/state":
            if self.hold_requests:
                self.held_routes.append(route)
            else:
                route.fulfill(
                    content_type="application/json", body=json.dumps(self.state)
                )
        elif route.request.url == "http://console.test/api/open_browser":
            route.fulfill(content_type="application/json", body="{}")
        else:
            route.abort()

    def refresh(self):
        self.page.evaluate("() => fetchState()")

    def begin_overlapping_requests(self):
        self.hold_requests = True
        self.page.evaluate(
            "() => { window.olderRequest = fetchState(); window.newerRequest = fetchState(); }"
        )
        deadline = time.monotonic() + 5
        while len(self.held_routes) < 2 and time.monotonic() < deadline:
            self.page.wait_for_timeout(10)
        self.assertEqual(len(self.held_routes), 2)

    def finish_request(self, index, state=None, status=200):
        self.held_routes[index].fulfill(
            status=status,
            content_type="application/json",
            body=json.dumps(state or self.state),
        )
        self.page.evaluate(
            f"() => window.{'olderRequest' if index == 0 else 'newerRequest'}"
        )

    def test_unchanged_rows_and_logs_are_not_recreated(self):
        self.page.evaluate("""() => {
          window.savedRows = [...document.querySelector('#accountsTbody').children];
          window.savedLog = document.querySelector('#logTerminal').firstChild;
          document.querySelector('#accountsTbody .select-trigger').focus();
        }""")
        for _ in range(5):
            self.refresh()
        self.assertTrue(
            self.page.evaluate("""() =>
          window.savedRows.every((row, index) => row === document.querySelector('#accountsTbody').children[index])
          && window.savedLog === document.querySelector('#logTerminal').firstChild
          && document.activeElement === window.savedRows[0].querySelector('.select-trigger')""")
        )
        self.assertEqual(self.page.evaluate("window.uiTestIntervals"), [1500])

    def test_only_changed_account_row_is_replaced(self):
        self.page.evaluate(
            "window.savedRows = [...document.querySelector('#accountsTbody').children]"
        )
        self.state["accounts"][1].update(
            status="running",
            current_step=2,
            is_running=True,
            totp_secret="TEST",
            totp_now="123456",
            totp_rem=20,
        )
        self.refresh()
        self.assertTrue(
            self.page.evaluate(
                "window.savedRows[0] === document.querySelector('#accountsTbody').children[0]"
            )
        )
        self.assertFalse(
            self.page.evaluate(
                "window.savedRows[1] === document.querySelector('#accountsTbody').children[1]"
            )
        )
        self.assertEqual(
            self.page.locator(".totp-pill span").first.inner_text(), "123456"
        )
        self.assertEqual(self.page.locator(".totp-rem").inner_text(), "20s")
        self.assertEqual(self.page.locator(".status-running").count(), 1)
        self.state["accounts"][1]["totp_rem"] = 18
        self.refresh()
        self.assertEqual(self.page.locator(".totp-rem").inner_text(), "18s")
        self.assertTrue(
            self.page.evaluate(
                "window.savedRows[0] === document.querySelector('#accountsTbody').children[0]"
            )
        )

    def test_repeated_row_updates_do_not_accumulate_text_nodes(self):
        self.page.evaluate("""() => {
          for (let index = 0; index < 40; index++) {
            renderAccounts([
              { id: 'first', email: 'first@example.invalid', password: 'test-only', status: 'pending' },
              { id: 'second', email: 'second@example.invalid', password: 'test-only', status: 'running', step_message: `Update ${index}` },
            ]);
          }
        }""")
        self.assertEqual(
            self.page.locator("#accountsTbody").evaluate("el => el.childNodes.length"),
            2,
        )

    def test_row_add_remove_reorder_duplicate_ids_and_empty_state(self):
        scenarios = [
            [account("first"), account("second"), account("third")],
            [account("third"), account("first")],
            [
                account("same", email="a@example.invalid"),
                account("same", email="b@example.invalid"),
            ],
            [],
            [account("new")],
            [],
        ]
        for accounts in scenarios:
            self.state["accounts"] = accounts
            self.refresh()
            self.assertEqual(
                self.page.locator("#accountsTbody tr").count(), max(len(accounts), 1)
            )
            self.assertEqual(
                self.page.locator(".account-email").all_text_contents(),
                [item["email"] for item in accounts],
            )
            self.assertEqual(
                self.page.locator(".empty-state").count(), int(not accounts)
            )

    def test_local_button_feedback_and_step_selection_remain_visible(self):
        self.page.evaluate("""() => {
          selectedStartSteps.first = 5;
          Object.defineProperty(navigator, 'clipboard', { value: {
            writeText: text => { window.copiedText = text; return Promise.resolve(); }
          }});
        }""")
        self.refresh()
        first = self.page.locator("#accountsTbody tr").first
        self.assertEqual(first.locator("select").input_value(), "5")
        first.get_by_role("button", name="More actions for first").scroll_into_view_if_needed()
        self.page.evaluate("""() => new Promise(resolve =>
          requestAnimationFrame(() => requestAnimationFrame(resolve)))""")
        first.get_by_role("button", name="More actions for first").focus()
        self.page.keyboard.press("ArrowDown")
        expect(self.page.get_by_role("menuitem", name="Open browser")).to_be_focused()
        self.page.keyboard.press("ArrowDown")
        expect(self.page.get_by_role("menuitem", name="Copy account")).to_be_focused()
        self.page.keyboard.press("Enter")
        self.page.wait_for_function("copiedAccountMap.first === true")
        self.assertEqual(self.page.evaluate("window.copiedText"), "first@example.invalid----test-only----")
        first.get_by_role("button", name="More actions for first").focus()
        self.page.keyboard.press("ArrowDown")
        expect(self.page.get_by_role("menuitem", name="Open browser")).to_be_focused()
        self.page.keyboard.press("Enter")
        self.page.wait_for_function("openedBrowserMap.first === true")
        self.refresh()
        self.assertTrue(first.get_by_role("status").is_visible())
        self.assertEqual(first.get_by_role("status").inner_text(), "Copied · Browser opened")
        first.get_by_role("button", name="More actions for first").click()
        self.assertTrue(self.page.get_by_role("menuitem", name="Copied").is_visible())
        self.assertTrue(self.page.get_by_role("menuitem", name="Browser opened").is_visible())
        self.page.keyboard.press("Escape")
        self.page.evaluate(
            "delete copiedAccountMap.first; delete openedBrowserMap.first;"
        )
        self.refresh()
        self.assertEqual(first.get_by_role("status").inner_text(), "")
        first.get_by_role("button", name="More actions for first").click()
        self.assertTrue(self.page.get_by_role("menuitem", name="Copy account").is_visible())
        self.assertTrue(self.page.get_by_role("menuitem", name="Open browser").is_visible())
        self.assertEqual(first.locator("select").input_value(), "5")

    def test_hugeicons_render_for_gift_logs_and_proxy_result(self):
        gift = self.page.locator(".sidebar-footer svg.local-avatar")
        self.assertTrue(gift.is_visible())
        self.assertEqual(gift.locator("use").get_attribute("href"), "#huge-gift")
        self.assertEqual(gift.evaluate("el => getComputedStyle(el).maskImage"), "none")
        self.assertTrue(self.page.evaluate(
            "document.querySelector('#huge-gift path').getAttribute('d').length > 0"))
        self.state["logs"] = ["[OK] Complete", "[WARN] Waiting", "[ERROR] Failed", "Ready"]
        self.refresh()
        self.assertEqual(
            self.page.locator(".log-line use").evaluate_all(
                "nodes => nodes.map(node => node.getAttribute('href'))"),
            ["#huge-ok", "#huge-warn", "#huge-error", "#huge-info"],
        )
        self.page.evaluate("""() => renderProxyResult(
            document.querySelector('#proxyTestResult'), 'error',
            '<img src=x onerror="window.resultInjected=true">')""")
        self.assertEqual(self.page.locator("#proxyTestResult use").get_attribute("href"), "#huge-error")
        self.assertEqual(self.page.locator("#proxyTestResult img").count(), 0)
        self.assertIn("<img", self.page.locator("#proxyTestResult").inner_text())
        self.assertFalse(self.page.evaluate("Boolean(window.resultInjected)"))

    def test_log_styles_escaping_and_scroll_behavior_unchanged(self):
        self.state["logs"] = [f"Log line {index}" for index in range(100)]
        self.refresh()
        self.page.evaluate("document.querySelector('#logTerminal').scrollTop = 0")
        self.state["logs"].extend(
            [
                "[WARN] Waiting for verification",
                "[ERROR] Error",
                "[OK] Complete",
                "<script>window.logInjected = true</script>",
            ]
        )
        self.refresh()
        self.assertEqual(self.page.locator(".log-warn").count(), 1)
        self.assertEqual(self.page.locator(".log-err").count(), 1)
        self.assertEqual(self.page.locator(".log-ok").count(), 1)
        self.assertEqual(
            self.page.locator("#logTerminal").evaluate("el => el.scrollTop"), 0
        )
        self.assertFalse(self.page.evaluate("Boolean(window.logInjected)"))
        self.page.evaluate("document.querySelector('#logTerminal').scrollTop = 99999")
        self.state["logs"].append("New log")
        self.refresh()
        self.assertTrue(
            self.page.locator("#logTerminal").evaluate(
                "el => el.scrollHeight - el.clientHeight <= el.scrollTop + 1"
            )
        )
        self.state["logs"] = []
        self.refresh()
        self.assertEqual(self.page.locator("#logTerminal").inner_html(), "")

    def test_older_success_cannot_overwrite_newer_success(self):
        self.begin_overlapping_requests()
        newer = deepcopy(self.state)
        newer["accounts"].append(account("third"))
        self.finish_request(1, newer)
        self.finish_request(0)
        self.assertEqual(self.page.locator("#statTotal").inner_text(), "3")
        self.assertEqual(self.page.locator(".account-email").count(), 3)

    def test_older_failure_cannot_overwrite_newer_connection(self):
        self.begin_overlapping_requests()
        self.finish_request(1)
        self.finish_request(0, status=503)
        self.assertIn(
            "Local service connected",
            self.page.locator("#connectionStatus").inner_text(),
        )

    def test_older_response_can_display_while_newer_request_is_pending(self):
        self.begin_overlapping_requests()
        older = deepcopy(self.state)
        older["accounts"].append(account("third"))
        self.finish_request(0, older)
        self.assertEqual(self.page.locator("#statTotal").inner_text(), "3")
        self.finish_request(1)
        self.assertEqual(self.page.locator("#statTotal").inner_text(), "2")

    def test_latest_failure_and_recovery_preserve_form_edits(self):
        self.page.locator("#cfgHolder").fill("UNSAVED UI TEST")
        self.begin_overlapping_requests()
        self.finish_request(1, status=503)
        self.finish_request(0)
        self.assertIn(
            "Connection lost · Retrying",
            self.page.locator("#connectionStatus").inner_text(),
        )
        self.hold_requests = False
        self.refresh()
        self.assertIn(
            "Local service connected",
            self.page.locator("#connectionStatus").inner_text(),
        )
        self.assertEqual(
            self.page.locator("#cfgHolder").input_value(), "UNSAVED UI TEST"
        )
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
