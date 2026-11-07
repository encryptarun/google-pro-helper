import asyncio
import importlib.util
import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


class StubAutomationEngine(types.ModuleType):
    def __init__(self, base_dir: Path, screenshots: Path):
        super().__init__("automation_engine")
        self.BASE_DIR = str(base_dir)
        self.SCREENSHOTS_DIR = str(screenshots)
        self.CHROME_PATH = "/stub/chrome"
        self.GoogleUltraAutomator = type("GoogleUltraAutomator", (), {})
        self.LocalHttpProxyAuthBridge = mock.Mock()
        self.get_profile_dir = mock.Mock(return_value=str(base_dir / "profile"))
        self.get_totp = mock.Mock(return_value="123456")
        self.parse_proxy_string = mock.Mock(return_value=None)
        self.resolve_account_proxy = mock.Mock(return_value="http://proxy.invalid")


class AppDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        base_dir = Path(self.directory.name)
        screenshots = base_dir / "screenshots"
        screenshots.mkdir()

        engine = StubAutomationEngine(base_dir, screenshots)

        self.old_engine = sys.modules.get("automation_engine")
        self.old_app = sys.modules.get("app_diagnostics_under_test")
        self.addCleanup(self._restore_modules)
        sys.modules["automation_engine"] = engine
        spec = importlib.util.spec_from_file_location("app_diagnostics_under_test", APP_PATH)
        if spec is None or spec.loader is None:
            raise ImportError(f"Cannot load app module from {APP_PATH}")
        self.app = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = self.app
        spec.loader.exec_module(self.app)

    def _restore_modules(self):
        if self.old_engine is None:
            sys.modules.pop("automation_engine", None)
        else:
            sys.modules["automation_engine"] = self.old_engine
        if self.old_app is None:
            sys.modules.pop("app_diagnostics_under_test", None)
        else:
            sys.modules["app_diagnostics_under_test"] = self.old_app

    def set_config(self, config):
        patcher = mock.patch.object(self.app, "load_config", return_value=config)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def test_index_reads_template_on_worker_thread(self):
        path = Path(self.directory.name) / "templates" / "index.html"
        path.parent.mkdir(exist_ok=True)
        path.write_text("<main>test</main>", encoding="utf-8")
        patcher = mock.patch.object(self.app, "TEMPLATES_DIR", str(path.parent))
        patcher.start()
        self.addCleanup(patcher.stop)
        with mock.patch.object(asyncio, "to_thread", wraps=asyncio.to_thread) as to_thread:
            self.assertEqual(await self.app.index(), "<main>test</main>")
        to_thread.assert_awaited_once()

    async def test_state_handles_invalid_totp_and_propagates_unexpected_error(self):
        self.set_config({"accounts": [{"id": "a", "totp_secret": "bad"}]})
        self.app.get_totp.side_effect = ValueError("invalid secret")
        with self.assertLogs("app_diagnostics_under_test", level="WARNING"):
            state = await self.app.get_state()
        self.assertEqual(state["accounts"][0]["totp_now"], "------")

        self.app.get_totp.side_effect = RuntimeError("bug")
        with self.assertRaisesRegex(RuntimeError, "bug"):
            await self.app.get_state()

    async def test_state_formats_expiry_as_iso_date(self):
        for raw, expected in (
            ("2028-10-08", "2028-10-08"),
            ("2028.10.8", "2028-10-08"),
            ("8 October 2028", "2028-10-08"),
            ("October 8, 2028", "2028-10-08"),
            ("08/10/2028", "2028-10-08"),
            ("-", "-"),
        ):
            with self.subTest(raw=raw):
                self.set_config({"accounts": [{"id": "a", "expiry_date": raw}]})
                state = await self.app.get_state()
                self.assertEqual(state["accounts"][0]["expiry_date"], expected)

    async def test_proxy_probe_errors_return_failure(self):
        self.set_config({"floppy_api_key": ""})
        request = self.app.ProxyTestReq(proxy_template="http://proxy.invalid")
        with mock.patch("urllib.request.build_opener", side_effect=OSError("offline")):
            result = await self.app.test_proxy(request)
        self.assertFalse(result["ok"])

        body = mock.Mock(read=mock.Mock(return_value=b"[]"))
        opener = mock.Mock(open=mock.Mock(return_value=body))
        with mock.patch("urllib.request.build_opener", return_value=opener):
            result = await self.app.test_proxy(request)
        self.assertFalse(result["ok"])

        body.read.return_value = b"{invalid"
        with mock.patch("urllib.request.build_opener", return_value=opener):
            result = await self.app.test_proxy(request)
        self.assertFalse(result["ok"])

    async def test_balance_failures_warn_but_ip_probe_succeeds(self):
        self.set_config({"floppy_api_key": "key"})
        request = self.app.ProxyTestReq(proxy_template="http://proxy.invalid")
        body = mock.Mock(read=mock.Mock(return_value=json.dumps({"ip": "1.2.3.4"}).encode()))
        opener = mock.Mock(open=mock.Mock(return_value=body))
        with (
            mock.patch("urllib.request.build_opener", return_value=opener),
            mock.patch("urllib.request.urlopen", side_effect=OSError("balance offline")),
            self.assertLogs("app_diagnostics_under_test", level="WARNING"),
        ):
            result = await self.app.test_proxy(request)
        self.assertTrue(result["ok"])
        self.assertIsNone(result["balance_gb"])

        with (
            mock.patch("urllib.request.build_opener", return_value=opener),
            mock.patch("urllib.request.urlopen", return_value=mock.Mock(read=mock.Mock(return_value=b"[]"))),
            self.assertLogs("app_diagnostics_under_test", level="WARNING"),
        ):
            result = await self.app.test_proxy(request)
        self.assertTrue(result["ok"])

        nested_payload = json.dumps({"proxy": {"rotating": None}}).encode()
        with (
            mock.patch("urllib.request.build_opener", return_value=opener),
            mock.patch(
                "urllib.request.urlopen",
                return_value=mock.Mock(read=mock.Mock(return_value=nested_payload)),
            ),
            self.assertLogs("app_diagnostics_under_test", level="WARNING"),
        ):
            result = await self.app.test_proxy(request)
        self.assertTrue(result["ok"])

    async def test_unexpected_probe_error_propagates(self):
        self.set_config({"floppy_api_key": ""})
        request = self.app.ProxyTestReq(proxy_template="http://proxy.invalid")
        with (
            mock.patch("urllib.request.build_opener", side_effect=RuntimeError("unexpected")),
            self.assertRaisesRegex(RuntimeError, "unexpected"),
        ):
            await self.app.test_proxy(request)

    async def test_bridge_stop_oserror_warns_and_browser_launches(self):
        self.set_config({"accounts": [{"id": "a", "email": "a@example.invalid"}]})
        self.app.parse_proxy_string.return_value = {
            "scheme": "http", "host": "proxy.invalid", "port": 8080,
            "username": "user", "password": "pass",
        }
        bridge = mock.Mock(stop=mock.AsyncMock(side_effect=OSError("stop failed")))
        self.app.MANUAL_BRIDGES["a"] = bridge
        replacement = mock.Mock(start=mock.AsyncMock())
        patcher = mock.patch.object(self.app, "LocalHttpProxyAuthBridge", return_value=replacement)
        patcher.start()
        self.addCleanup(patcher.stop)
        with (
            mock.patch.object(self.app, "_launch_browser") as launch,
            self.assertLogs("app_diagnostics_under_test", level="WARNING"),
        ):
            result = await self.app.open_browser_manual(self.app.AccountActionReq(account_id="a"))
        self.assertTrue(result["ok"])
        launch.assert_called_once()
        replacement.start.assert_awaited_once()

    async def test_manual_browser_uses_english_us_locale(self):
        self.set_config({"accounts": [{"id": "a", "email": "a@example.invalid"}]})
        with mock.patch.object(self.app, "_launch_browser") as launch:
            await self.app.open_browser_manual(self.app.AccountActionReq(account_id="a"))
        args = launch.call_args.args[0]
        self.assertIn("--lang=en-US", args)
        urls = [arg for arg in args if arg.startswith("https://")]
        self.assertEqual(len(urls), 3)
        self.assertTrue(all("hl=en-US" in url for url in urls))

    async def test_unexpected_bridge_stop_error_propagates(self):
        self.set_config({"accounts": [{"id": "a", "email": "a@example.invalid"}]})
        self.app.parse_proxy_string.return_value = {
            "scheme": "http", "host": "proxy.invalid", "port": 8080,
            "username": "user", "password": "pass",
        }
        self.app.MANUAL_BRIDGES["a"] = mock.Mock(
            stop=mock.AsyncMock(side_effect=RuntimeError("unexpected stop"))
        )
        with (
            mock.patch.object(self.app, "_launch_browser"),
            self.assertRaisesRegex(RuntimeError, "unexpected stop"),
        ):
            await self.app.open_browser_manual(self.app.AccountActionReq(account_id="a"))


if __name__ == "__main__":
    unittest.main()
