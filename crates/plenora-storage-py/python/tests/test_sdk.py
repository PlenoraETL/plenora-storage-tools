import asyncio
import importlib.metadata
import inspect
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
import tempfile
import threading
import traceback
import unittest
from types import MappingProxyType

import plenora_storage
from plenora_storage import AsyncEngine, CancellationToken, Connection, Engine, EngineConfig, PlenoraError, StorageError, cancellation_outcome, version


class SDKTests(unittest.TestCase):
    def test_installed_identity_typing_and_public_surface(self):
        distribution = importlib.metadata.distribution('plenora-storage')
        self.assertEqual(version(), distribution.version)
        self.assertEqual(plenora_storage.__version__, version())
        installed = Path(distribution.locate_file('plenora_storage')).resolve()
        self.assertEqual(Path(plenora_storage.__file__).resolve().parent, installed)
        self.assertEqual(Path(plenora_storage._native.__file__).resolve().parent, installed)
        self.assertTrue((installed / 'py.typed').is_file())
        self.assertTrue((installed / '_native.pyi').is_file())
        self.assertTrue(issubclass(StorageError, PlenoraError))
        self.assertEqual(set(plenora_storage.__all__), {'Engine', 'AsyncEngine', 'EngineConfig', 'Connection',
                         'PlenoraError', 'StorageError', 'CancellationToken', 'cancellation_outcome', 'version', '__version__'})
        for operation in ['test', 'list', 'stat', 'get', 'put', 'copy', 'delete']:
            self.assertEqual(inspect.signature(getattr(Engine, operation)),
                             inspect.signature(getattr(AsyncEngine, operation)))

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

    def test_immutable_credential_mapping_is_resolved_only_on_use(self):
        calls = []
        def resolver(reference):
            calls.append(reference)
            return MappingProxyType({'username': 'fixture', 'password': 'fixture'})
        connection = Connection('webdav', 'plenora-storage-webdav-connection-v1',
                                {'endpoint': 'http://127.0.0.1:9/'}, 'vault:fixture')
        with Engine(EngineConfig(allow_insecure_http=True, allow_private_network=True),
                    credential_resolver=resolver) as engine:
            engine.capabilities()
            self.assertEqual(calls, [])
            try:
                engine.test(connection, timeout_ms=1000)
            except StorageError as error:
                self.assertNotEqual(error.code, 'CREDENTIAL_RESOLVER_FAILED')
            self.assertEqual(calls, ['vault:fixture'])

    def test_limits_reject_upload_without_publication(self):
        with Engine(EngineConfig(max_transfer_bytes=1)) as engine, self.assertRaises(StorageError) as caught:
            engine.put(self.connection, 'too-large', self.source, overwrite=False,
                       publication_policy='atomic_required')
        self.assertEqual(caught.exception.category, 'resource_limit')
        self.assertFalse((self.storage / 'too-large').exists())

    def test_full_catalog(self):
        catalog = self.engine.capabilities()
        self.assertEqual(catalog['component_version'], plenora_storage._native.__version__)
        self.assertEqual(catalog['interfaces'], [{'kind': 'python_sdk', 'contract': 'plenora-python-sdk-v1',
                                                'version': 1, 'artifact': 'plenora-storage'}])
        self.assertTrue(all(operation['surfaces'] == ['python_sdk'] for operation in catalog['operations']))
        self.assertEqual(len(catalog['operations']), 7)
        self.assertEqual({provider['provider'] for provider in catalog['operations'][0]['attributes']['providers']},
                         {'local', 's3', 'sftp', 'ftp', 'ftps', 'azure', 'gcs', 'smb', 'webdav'})

    def test_boundary_errors_are_typed_redacted_and_have_no_effect(self):
        class BadPath:
            def __fspath__(self):
                raise ValueError('sentinel-path-secret')
        calls = [lambda: self.engine.test(self.connection, timeout_ms=2**64),
                 lambda: self.engine.test(self.connection, timeout_ms=True),
                 lambda: self.engine.test(self.connection, cancellation='sentinel-token-secret'),
                 lambda: self.engine.test(replace(self.connection, config='sentinel-config-secret')),
                 lambda: self.engine.get(self.connection, 'absent', BadPath(), overwrite=True),
                 lambda: Engine('sentinel-config-secret'),
                 lambda: Engine(credential_resolver='sentinel-resolver-secret')]
        for call in calls:
            try:
                call()
            except PlenoraError as error:
                self.assertEqual(error.remote_effect, 'none')
                self.assertEqual(error.retry, {'kind': 'never'})
                self.assertNotIn('sentinel-', str(error))
                self.assertNotIn('sentinel-', repr(error))
                self.assertNotIn('sentinel-path-secret', ''.join(traceback.format_exception(error)))
            else:
                self.fail('invalid SDK input accepted')
        self.assertEqual(list(self.storage.iterdir()), [])

    def test_closed_context_uses_same_axes_as_operations(self):
        self.engine.close()
        errors = []
        for call in [self.engine.__enter__, lambda: self.engine.test(self.connection)]:
            with self.assertRaises(PlenoraError) as caught:
                call()
            errors.append(vars(caught.exception))
        self.assertEqual(errors[0], errors[1])

    def test_local_artifact_errors_preserve_phase_and_no_effect(self):
        missing = self.root / 'sentinel-private-missing'
        for call, code, phase in [
            (lambda: self.engine.put(self.connection, 'new', missing, overwrite=True,
                                    publication_policy='atomic_required'), 'INPUT_METADATA_FAILED', 'read'),
            (lambda: self.engine.get(self.connection, 'absent', missing / 'output', overwrite=True),
             'OUTPUT_STAGING_CREATE_FAILED', 'prepare'),
        ]:
            with self.assertRaises(StorageError) as caught:
                call()
            error = caught.exception
            self.assertEqual((error.code, error.category, error.phase), (code, 'not_found', phase))
            self.assertEqual(error.remote_effect, 'none')
            self.assertEqual(error.retry, {'kind': 'never'})
            self.assertNotIn('sentinel-private', str(error))
        self.assertEqual(list(self.storage.iterdir()), [])

    def test_config_and_document_callbacks_cannot_escape_as_public_errors(self):
        class BadValue:
            def __deepcopy__(self, _memo):
                raise RuntimeError('sentinel-deepcopy-secret')

        class BadDict(dict):
            def items(self):
                raise RuntimeError('sentinel-document-secret')

        class BadConfig(EngineConfig):
            def __getattribute__(self, name):
                if name == 'max_transfer_bytes':
                    raise RuntimeError('sentinel-config-secret')
                return super().__getattribute__(name)

        for call in [lambda: Engine(EngineConfig(max_transfer_bytes=BadValue())),
                     lambda: Engine(BadConfig()),
                     lambda: self.engine.stat(self.connection, BadDict(value=1))]:
            with self.assertRaises(StorageError) as caught:
                call()
            error = caught.exception
            self.assertEqual(error.code, 'SDK_INPUT_INVALID')
            self.assertEqual(error.remote_effect, 'none')
            self.assertEqual(error.retry, {'kind': 'never'})
            self.assertNotIn('sentinel-', ''.join(traceback.format_exception(error)))


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_file_and_serialization_errors_match_sync(self):
        class BadDict(dict):
            def items(self):
                raise RuntimeError('sentinel-document-secret')

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            connection = Connection('local', 'plenora-storage-local-connection-v1',
                                    {'root': str(root)}, 'local:process')
            calls = [
                ('put', (connection, 'new', root / 'missing'),
                 {'overwrite': True, 'publication_policy': 'atomic_required'}),
                ('get', (connection, 'absent', root / 'missing' / 'download'), {'overwrite': True}),
                ('stat', (connection, BadDict(value=1)), {}),
            ]
            with Engine() as sync:
                async with AsyncEngine() as asynchronous:
                    for method, args, kwargs in calls:
                        with self.assertRaises(StorageError) as expected:
                            getattr(sync, method)(*args, **kwargs)
                        with self.assertRaises(StorageError) as actual:
                            await getattr(asynchronous, method)(*args, **kwargs)
                        self.assertEqual(vars(expected.exception), vars(actual.exception))
                        self.assertNotIn('sentinel-', ''.join(traceback.format_exception(actual.exception)))
            self.assertEqual(list(root.iterdir()), [])

    async def test_cancelled_success_survives_task_and_timeout_wrappers(self):
        # Model an operation that committed just as cancellation arrived.
        for wrapper in ['direct', 'wait_for', 'timeout']:
            with self.subTest(wrapper=wrapper):
                entered, release = threading.Event(), threading.Event()
                outcome = {'committed': True}
                def committed(*_args, **_kwargs):
                    entered.set()
                    release.wait(5)
                    return outcome
                async with AsyncEngine() as engine:
                    engine._engine.test = committed
                    task = asyncio.create_task(engine.test(None))
                    self.assertTrue(await asyncio.to_thread(entered.wait, 5))
                    if wrapper == 'timeout':
                        asyncio.get_running_loop().call_later(0.05, release.set)
                        with self.assertRaises(asyncio.TimeoutError) as caught:
                            await asyncio.wait_for(task, 0.01)
                    else:
                        task.cancel()
                        await asyncio.sleep(0)
                        release.set()
                        with self.assertRaises(asyncio.CancelledError) as caught:
                            if wrapper == 'direct':
                                await task
                            else:
                                await asyncio.wait_for(task, 5)
                    self.assertEqual(cancellation_outcome(caught.exception), outcome)
                    self.assertTrue(task.done())

    async def test_unknown_cancellation_outcome_is_not_no_effect(self):
        error = asyncio.CancelledError()
        error.__context__ = error
        self.assertIsNone(cancellation_outcome(error))
        self.assertIsNone(cancellation_outcome(asyncio.TimeoutError()))

    async def test_async_lifecycle(self):
        with tempfile.TemporaryDirectory() as root:
            connection = Connection('local', 'plenora-storage-local-connection-v1', {'root': root}, 'local:process')
            async with AsyncEngine() as engine:
                await engine.test(connection)
                self.assertEqual((await engine.list(connection))['objects'], [])
            self.assertTrue(engine.is_closed)
            await engine.aclose()
            await engine.close()
            with self.assertRaises(PlenoraError):
                await engine.__aenter__()
            with self.assertRaises(PlenoraError) as caught:
                await engine.test(connection)
            self.assertEqual(caught.exception.code, 'ENGINE_CLOSED')

    async def test_async_invalid_controls_match_sync(self):
        connection = Connection('local', 'plenora-storage-local-connection-v1', {'root': '.'}, 'local:process')
        for controls in [{'timeout_ms': -1}, {'timeout_ms': 2**64}, {'cancellation': object()}]:
            with Engine() as sync, self.assertRaises(PlenoraError) as synchronous:
                sync.test(connection, **controls)
            async with AsyncEngine() as asynchronous:
                with self.assertRaises(PlenoraError) as caught:
                    await asynchronous.test(connection, **controls)
            self.assertEqual(vars(synchronous.exception), vars(caught.exception))

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
            await asyncio.wait_for(engine.aclose(), 1)
            self.assertTrue(engine.is_closed)
            task.cancel()
            await asyncio.sleep(0.02)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError) as caught:
                await asyncio.wait_for(task, 5)
            self.assertEqual(cancellation_outcome(caught.exception).category, 'cancelled')


if __name__ == '__main__':
    unittest.main()
