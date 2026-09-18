import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import unittest

from plenora_storage import AsyncEngine, CancellationToken, Connection, Engine, EngineConfig, StorageError


class SDKTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.storage = self.root / 'storage'
        self.storage.mkdir()
        self.connection = Connection('local', 'plenora-storage-local-connection-v1',
                                     {'root': str(self.storage)}, 'local:process')
        self.engine = Engine()
        self.addCleanup(self.engine.close)
        self.source = self.root / 'input.bin'
        self.source.write_bytes(bytes(range(256)) * 1024)

    def put(self, key='source.bin'):
        return self.engine.put(self.connection, key, self.source, overwrite=False,
                               publication_policy='atomic_required')

    def test_all_operations_and_integrity(self):
        self.engine.test(self.connection)
        self.put()
        self.assertEqual(self.engine.stat(self.connection, 'source.bin')['size'], self.source.stat().st_size)
        self.engine.copy(self.connection, 'source.bin', 'copy.bin', overwrite=False,
                         publication_policy='atomic_required')
        self.assertEqual(len(self.engine.list(self.connection)['objects']), 2)
        target = self.root / 'download.bin'
        self.engine.get(self.connection, 'copy.bin', target, overwrite=False)
        self.assertEqual(target.read_bytes(), self.source.read_bytes())
        self.engine.delete(self.connection, 'copy.bin', ignore_missing=False)
        with self.assertRaises(StorageError) as caught:
            self.engine.stat(self.connection, 'copy.bin')
        self.assertEqual(caught.exception.category, 'not_found')

    def test_cursor_is_engine_owned(self):
        for key in ['a', 'b', 'c']:
            self.put(key)
        first = self.engine.list(self.connection, max_items=1)
        self.assertTrue(first['truncated'])
        second = self.engine.list(self.connection, max_items=1, cursor=first['next_cursor'])
        self.assertNotEqual(first['objects'], second['objects'])
        with Engine() as other, self.assertRaises(StorageError):
            other.list(self.connection, max_items=1, cursor=first['next_cursor'])

    def test_no_clobber_and_failed_download_cleanup(self):
        self.put()
        target = self.root / 'existing'
        target.write_bytes(b'previous content')
        with self.assertRaises(StorageError) as caught:
            self.engine.get(self.connection, 'source.bin', target, overwrite=False)
        self.assertEqual(caught.exception.category, 'conflict')
        self.assertEqual(target.read_bytes(), b'previous content')
        with self.assertRaises(StorageError):
            self.engine.get(self.connection, 'absent', target, overwrite=True)
        self.assertEqual(target.read_bytes(), b'previous content')
        self.assertEqual(list(self.root.glob('*.part')), [])

    def test_concurrent_downloads_use_distinct_staging(self):
        self.put()
        def download(index):
            output = self.root / f'output-{index}'
            self.engine.get(self.connection, 'source.bin', output, overwrite=False)
            return output.read_bytes()
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertTrue(all(value == self.source.read_bytes() for value in pool.map(download, range(12))))
        same = self.root / 'concurrent-output'
        def replace_same(_index):
            return self.engine.get(self.connection, 'source.bin', same, overwrite=True)
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(len(list(pool.map(replace_same, range(12)))), 12)
        self.assertEqual(same.read_bytes(), self.source.read_bytes())

    def test_deadline_and_cancellation_have_no_file_effect(self):
        token = CancellationToken()
        token.cancel()
        self.assertTrue(token.is_cancelled)
        for controls, category in [({'cancellation': token}, 'cancelled'), ({'timeout_ms': 0}, 'timeout')]:
            target = self.root / 'never-created'
            with self.assertRaises(StorageError) as caught:
                self.engine.get(self.connection, 'absent', target, overwrite=False, **controls)
            self.assertEqual(caught.exception.category, category)
            self.assertEqual(caught.exception.remote_effect, 'none')
            self.assertFalse(target.exists())
        self.assertEqual(list(self.root.glob('*.part')), [])

    def test_closed_engine_and_invalid_path_before_filesystem_effect(self):
        target = self.root / 'never-created'
        for key in ['../escape', 'good']:
            if key == 'good':
                self.engine.close()
                self.engine.close()
                self.assertTrue(self.engine.is_closed)
            with self.assertRaises(StorageError):
                self.engine.get(self.connection, key, target, overwrite=False)
            self.assertFalse(target.exists())

    def test_inline_secrets_and_errors_are_redacted(self):
        sentinel = 'sensitive-payload-never-in-errors'
        connection = replace(self.connection, config={'password': sentinel})
        with self.assertRaises(StorageError) as caught:
            self.engine.test(connection)
        self.assertNotIn(sentinel, str(caught.exception))
        self.assertNotIn(sentinel, repr(connection))

    def test_resolver_failure_is_redacted(self):
        def resolver(_reference):
            raise RuntimeError('sentinel-secret-exception')
        connection = Connection('webdav', 'plenora-storage-webdav-connection-v1',
                                {'endpoint': 'http://127.0.0.1:9/'}, 'vault:fixture')
        with Engine(EngineConfig(allow_insecure_http=True, allow_private_network=True),
                    credential_resolver=resolver) as engine, self.assertRaises(StorageError) as caught:
            engine.test(connection)
        self.assertEqual(caught.exception.code, 'CREDENTIAL_RESOLVER_FAILED')
        self.assertNotIn('sentinel', str(caught.exception))

    def test_limits_reject_upload_without_publication(self):
        with Engine(EngineConfig(max_transfer_bytes=1)) as engine, self.assertRaises(StorageError) as caught:
            engine.put(self.connection, 'too-large', self.source, overwrite=False,
                       publication_policy='atomic_required')
        self.assertEqual(caught.exception.category, 'resource_limit')
        self.assertFalse((self.storage / 'too-large').exists())

    def test_full_catalog(self):
        catalog = self.engine.capabilities()
        self.assertEqual(len(catalog['operations']), 7)
        self.assertEqual({provider['provider'] for provider in catalog['operations'][0]['attributes']['providers']},
                         {'local', 's3', 'sftp', 'ftp', 'ftps', 'azure', 'gcs', 'smb', 'webdav'})


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_async_lifecycle(self):
        with tempfile.TemporaryDirectory() as root:
            connection = Connection('local', 'plenora-storage-local-connection-v1', {'root': root}, 'local:process')
            async with AsyncEngine() as engine:
                await engine.test(connection)
                self.assertEqual((await engine.list(connection))['objects'], [])
            self.assertTrue(engine.is_closed)

    async def test_cancellation_drains_blocked_resolver_without_blocking_event_loop(self):
        entered = threading.Event()
        release = threading.Event()
        def resolver(_reference):
            entered.set()
            release.wait(5)
            return {'username': 'fixture', 'password': 'fixture'}
        connection = Connection('webdav', 'plenora-storage-webdav-connection-v1',
                                {'endpoint': 'http://127.0.0.1:9/'}, 'vault:fixture')
        async with AsyncEngine(EngineConfig(allow_insecure_http=True, allow_private_network=True),
                               credential_resolver=resolver) as engine:
            task = asyncio.create_task(engine.test(connection))
            self.assertTrue(await asyncio.to_thread(entered.wait, 5))
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(task, 5)
            self.assertEqual(caught.exception.storage_error.category, 'cancelled')


if __name__ == '__main__':
    unittest.main()
