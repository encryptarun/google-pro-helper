import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import HTTPException
from test_config_storage import load_storage_functions


class BatchSchedulingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.g = load_storage_functions(Path(self.directory.name))
        self.g['append_log'] = lambda line: None
        self.g['save_config']({'accounts': [
            {'id': 'Account_1', 'email': 'a@gmail.com', 'status': 'pending'},
            {'id': 'Account_2', 'email': 'b@gmail.com', 'status': 'pending'},
            {'id': 'Account_3', 'email': 'c@gmail.com', 'status': 'pending'},
        ]})
        self.initial_tasks = asyncio.all_tasks()

    async def asyncTearDown(self):
        pending = asyncio.all_tasks() - self.initial_tasks - {asyncio.current_task()}
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        self.directory.cleanup()

    async def test_delete_then_import_keeps_ids_unique(self):
        await self.g['delete_account'](self.g['AccountActionReq'](account_id='Account_2'))
        await self.g['import_accounts'](self.g['ImportAccountsReq'](
            raw_text='d@gmail.com----pw'))
        accounts = self.g['load_config']()['accounts']
        self.assertEqual(len({a['id'] for a in accounts}), 3)

    async def test_duplicate_explicit_label_rejected_without_saving(self):
        with self.assertRaises(HTTPException):
            await self.g['import_accounts'](self.g['ImportAccountsReq'](
                raw_text='Account_1----different@gmail.com----pw'))
        self.assertEqual(len(self.g['load_config']()['accounts']), 3)

    async def test_single_reserves_before_task_starts(self):
        event = asyncio.Event()
        async def worker(*args, **kwargs):
            await event.wait()
        self.g['_run_account_worker'] = worker
        req = self.g['RunSingleReq'](account_id='Account_1')
        await self.g['run_single'](req)
        with self.assertRaises(HTTPException):
            await self.g['run_single'](req)
        with self.assertRaises(HTTPException):
            await self.g['delete_account'](self.g['AccountActionReq'](account_id='Account_1'))
        event.set()
        await asyncio.sleep(0)
        self.assertEqual(self.g['RESERVED_RUNS'], {})

    async def test_port_allocator_skips_live_port_after_other_task_finishes(self):
        self.g['ACTIVE_TASKS']['Account_2'] = SimpleNamespace(port=9321)
        port = self.g['_reserve_run']('Account_1')
        self.assertEqual(port, 9320)
        other = self.g['_reserve_run']('Account_3')
        self.assertEqual(other, 9322)

    async def test_default_batch_excludes_running_accounts(self):
        self.g['ACTIVE_TASKS']['Account_1'] = SimpleNamespace(port=9320)
        result = await self.g['run_batch'](self.g['RunBatchReq']())
        self.assertEqual(result['queued'], ['Account_2', 'Account_3'])
        with self.assertRaises(HTTPException):
            await self.g['run_batch'](self.g['RunBatchReq']())
        self.g['STOP_BATCH'] = True

    async def test_explicit_batch_rejects_active_and_unknown_accounts(self):
        self.g['ACTIVE_TASKS']['Account_1'] = SimpleNamespace(port=9320)
        for aid in ('Account_1', 'missing'):
            with self.assertRaises(HTTPException):
                await self.g['run_batch'](self.g['RunBatchReq'](account_ids=[aid]))
        self.assertFalse(self.g['BATCH_RUNNING'])

    async def test_batch_skips_account_started_after_queue_created(self):
        calls = []
        wait = asyncio.Event()
        async def worker(account_id, **kwargs):
            calls.append(account_id)
            await wait.wait()
        self.g['_run_account_worker'] = worker
        await self.g['run_batch'](self.g['RunBatchReq']())
        # A single run wins the reservation before the batch coroutine starts.
        self.g['_reserve_run']('Account_1')
        await asyncio.sleep(0)
        self.assertEqual(calls, ['Account_2'])

    async def test_stop_immediately_after_batch_request_prevents_start(self):
        calls = []
        async def worker(account_id, **kwargs):
            calls.append(account_id)
        self.g['_run_account_worker'] = worker
        await self.g['run_batch'](self.g['RunBatchReq']())
        await self.g['stop_all']()
        await asyncio.sleep(0)
        self.assertEqual(calls, [])
        self.assertFalse(self.g['BATCH_RUNNING'])

    async def test_worker_failure_releases_reservation(self):
        async def worker(*args, **kwargs):
            raise RuntimeError('failure')
        self.g['_run_account_worker'] = worker
        port = self.g['_reserve_run']('Account_1')
        with self.assertRaisesRegex(RuntimeError, 'failure'):
            await self.g['_run_reserved_account']('Account_1', 1, port)
        self.assertEqual(self.g['RESERVED_RUNS'], {})

    async def test_single_stop_before_start_does_not_stop_other_account(self):
        other = SimpleNamespace(port=9320, stop_requested=False)
        self.g['ACTIVE_TASKS']['Account_2'] = other
        self.g['_reserve_run']('Account_1')
        await self.g['stop_all'](self.g['AccountActionReq'](account_id='Account_1'))
        self.assertIn('Account_1', self.g['STOP_REQUESTED_ACCOUNTS'])
        self.assertFalse(other.stop_requested)


if __name__ == '__main__':
    unittest.main()
