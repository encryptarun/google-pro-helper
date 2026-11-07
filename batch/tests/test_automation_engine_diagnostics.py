import asyncio
import importlib.util
import os
import signal
import unittest
from pathlib import Path
from unittest import mock

from playwright.async_api import Error as PlaywrightError

ENGINE_PATH = Path(__file__).resolve().parents[1] / "automation_engine.py"


def load_engine():
    spec = importlib.util.spec_from_file_location("engine_diagnostics", ENGINE_PATH)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load automation engine from {ENGINE_PATH}")
    engine = importlib.util.module_from_spec(spec)
    # Import the complete engine without requiring Chrome or creating profile folders.
    with (
        mock.patch.object(os.path, "exists", return_value=True),
        mock.patch.object(os, "makedirs"),
    ):
        spec.loader.exec_module(engine)
    return vars(engine)


class EngineDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.engine = load_engine()
        self.automator = self.engine["GoogleUltraAutomator"](
            {"id": "test", "email": "test@example.invalid"}, {}, on_log=lambda _: None
        )
        self.bridge = self.engine["LocalHttpProxyAuthBridge"](
            1234,
            {
                "host": "upstream.invalid",
                "port": 8080,
                "username": "user",
                "password": "pass",
            },
        )

    async def test_navigation_retries_then_succeeds(self):
        self.automator.proxy_bridge = mock.Mock()
        page = mock.Mock(
            goto=mock.AsyncMock(side_effect=[PlaywrightError("offline"), None])
        )
        with mock.patch.object(asyncio, "sleep", new_callable=mock.AsyncMock) as sleep:
            await self.automator.safe_goto(page, "https://example.invalid", timeout=123)
        self.assertEqual(page.goto.await_count, 2)
        page.goto.assert_awaited_with(
            "https://example.invalid", wait_until="domcontentloaded", timeout=123
        )
        self.automator.proxy_bridge.rotate_session.assert_called_once()
        sleep.assert_awaited_once_with(2.5)

    async def test_navigation_exhaustion_reraises_last_error(self):
        errors = [PlaywrightError(str(i)) for i in range(4)]
        page = mock.Mock(goto=mock.AsyncMock(side_effect=errors))
        self.automator.proxy_bridge = mock.Mock()
        with (
            mock.patch.object(asyncio, "sleep", new_callable=mock.AsyncMock),
            self.assertRaises(PlaywrightError) as raised,
        ):
            await self.automator.safe_goto(page, "https://example.invalid")
        self.assertIs(raised.exception, errors[-1])
        self.assertEqual(page.goto.await_count, 4)
        self.assertEqual(self.automator.proxy_bridge.rotate_session.call_count, 3)

    async def test_navigation_does_not_retry_unexpected_error(self):
        failure = ValueError("bad argument")
        page = mock.Mock(goto=mock.AsyncMock(side_effect=failure))
        with self.assertRaises(ValueError) as raised:
            await self.automator.safe_goto(page, "https://example.invalid")
        self.assertIs(raised.exception, failure)
        self.assertEqual(page.goto.await_count, 1)

    async def test_pipe_closes_writer_on_disconnect(self):
        reader = mock.Mock(
            at_eof=mock.Mock(return_value=False),
            read=mock.AsyncMock(side_effect=ConnectionResetError()),
        )
        writer = mock.Mock()
        await self.bridge._pipe(reader, writer)
        writer.close.assert_called_once()

    async def test_pipe_propagates_unexpected_errors_and_closes_writer(self):
        reader = mock.Mock(
            at_eof=mock.Mock(return_value=False),
            read=mock.AsyncMock(side_effect=ValueError("unexpected")),
        )
        writer = mock.Mock()
        with self.assertRaisesRegex(ValueError, "unexpected"):
            await self.bridge._pipe(reader, writer)
        writer.close.assert_called_once()

    async def test_failed_upstream_attempts_close_each_stream_and_client(self):
        client_reader = mock.Mock(
            readline=mock.AsyncMock(
                side_effect=[b"CONNECT example.invalid:443 HTTP/1.1\r\n", b"\r\n"]
            )
        )
        client_writer = mock.Mock(drain=mock.AsyncMock())
        upstream_writers = [mock.Mock(drain=mock.AsyncMock()) for _ in range(3)]
        upstream_readers = [
            mock.Mock(
                readuntil=mock.AsyncMock(
                    return_value=b"HTTP/1.1 502 Bad Gateway\r\n\r\n"
                )
            )
            for _ in range(3)
        ]
        with (
            mock.patch.object(
                asyncio,
                "open_connection",
                new_callable=mock.AsyncMock,
                side_effect=list(zip(upstream_readers, upstream_writers)),
            ),
            mock.patch.object(asyncio, "sleep", new_callable=mock.AsyncMock),
        ):
            await self.bridge._handle_client(client_reader, client_writer)
        for writer in upstream_writers:
            writer.close.assert_called_once()
        client_writer.close.assert_called_once()
        self.assertGreaterEqual(self.bridge.rotation_idx, 1)

    async def test_process_term_waits_for_reap(self):
        process = mock.Mock(
            pid=42, returncode=None, wait=mock.AsyncMock(return_value=0)
        )
        with mock.patch.object(os, "killpg") as kill:
            await self.engine["_stop_process_group"](process)
        kill.assert_called_once_with(42, signal.SIGTERM)
        process.wait.assert_awaited_once()

    async def test_process_timeout_kills_and_reaps(self):
        process = mock.Mock(
            pid=42, returncode=None, wait=mock.AsyncMock(return_value=0)
        )

        async def timeout(awaitable, timeout):
            awaitable.close()
            raise TimeoutError

        with (
            mock.patch.object(os, "killpg") as kill,
            mock.patch.object(asyncio, "wait_for", side_effect=timeout),
        ):
            await self.engine["_stop_process_group"](process)
        self.assertEqual(
            kill.call_args_list,
            [mock.call(42, signal.SIGTERM), mock.call(42, signal.SIGKILL)],
        )
        self.assertEqual(process.wait.await_count, 1)

    async def test_launch_failure_reports_error_and_stops_proxy(self):
        self.engine.update(
            get_profile_dir=lambda *_: "/dummy/profile",
            resolve_account_proxy=lambda *_: "proxy",
            parse_proxy_string=lambda _: {
                "host": "proxy.invalid",
                "port": 8080,
                "username": "user",
                "password": "pass",
            },
            _stop_processes_listening_on_port=lambda _: None,
        )
        bridge = mock.Mock(start=mock.AsyncMock(), stop=mock.AsyncMock())
        self.engine["LocalHttpProxyAuthBridge"] = mock.Mock(return_value=bridge)
        states = []
        self.automator.on_state = states.append
        with (
            mock.patch.object(asyncio, "sleep", new_callable=mock.AsyncMock),
            mock.patch.object(
                asyncio,
                "create_subprocess_exec",
                new_callable=mock.AsyncMock,
                side_effect=OSError("launch failed"),
            ) as launch,
            self.assertLogs("engine_diagnostics", level="ERROR"),
        ):
            await self.automator.run_pipeline(start_step=5)
        launch.assert_awaited_once()
        self.assertTrue(launch.call_args.kwargs["start_new_session"])
        bridge.stop.assert_awaited_once()
        self.assertEqual(states[-1]["status"], "error")
        self.assertIn("launch failed", states[-1]["step_message"])


if __name__ == "__main__":
    unittest.main()
