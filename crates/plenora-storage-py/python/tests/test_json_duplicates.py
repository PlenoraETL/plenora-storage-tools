import json
import unittest
from unittest.mock import Mock

import plenora_storage as sdk


class DuplicateJsonTests(unittest.TestCase):
    def test_native_output_ingresses_reject_recursive_duplicates(self):
        engine = object.__new__(sdk.Engine)
        engine._native = Mock()
        connection = sdk.Connection('local', 'local', {}, 'env:TEST')
        for document in ('{"PRIVATE_KEY":1,"PRIVATE_KEY":"PRIVATE_VALUE"}',
                         '{"outer":[{"PRIVATE_KEY":1,"PRIVATE_KEY":"PRIVATE_VALUE"}]}',
                         '{"key":1,"k\\u0065y":2}'):
            engine._native.capabilities.return_value = document
            engine._native.invoke.return_value = document
            for call in (engine.capabilities, lambda: engine.test(connection)):
                with self.subTest(call=call, document=document), self.assertRaises(sdk.StorageError) as raised:
                    call()
                self.assertEqual(raised.exception.code, 'SDK_JSON_DUPLICATE_MEMBER')
                self.assertEqual(raised.exception.remote_effect, 'unknown')
                self.assertNotIn('PRIVATE', str(raised.exception))
            engine._native.invoke.side_effect = ValueError(document)
            with self.assertRaises(sdk.StorageError) as raised:
                engine.test(connection)
            self.assertEqual(raised.exception.code, 'SDK_JSON_DUPLICATE_MEMBER')
            self.assertNotIn('PRIVATE', str(raised.exception))
            engine._native.invoke.side_effect = None

        clean = '{"items":[{"key":1},{"key":2}]}'
        engine._native.capabilities.return_value = clean
        engine._native.invoke.return_value = clean
        self.assertEqual(engine.capabilities(), json.loads(clean))
        self.assertEqual(engine.test(connection), json.loads(clean))
        error = {'code': 'TEST', 'category': 'internal', 'phase': 'prepare',
                 'remote_effect': 'unknown', 'retry': {'kind': 'requires_recovery'}, 'message': 'test'}
        engine._native.invoke.side_effect = ValueError(json.dumps(error))
        with self.assertRaises(sdk.StorageError) as raised:
            engine.test(connection)
        self.assertEqual(raised.exception.code, 'TEST')
