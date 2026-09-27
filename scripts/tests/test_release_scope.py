import copy
import sys
from pathlib import Path
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from release_scope import qualification_scope, validate_scope


class ReleaseScopeTests(unittest.TestCase):
    def test_fixture_evidence_cannot_claim_real_cloud_qualification(self):
        scope = qualification_scope()
        validate_scope(scope)
        for service in scope['real_cloud_services']:
            altered = copy.deepcopy(scope)
            altered['real_cloud_services'][service] = 'PASS'
            with self.subTest(service=service), self.assertRaises(ValueError):
                validate_scope(altered)
        missing = copy.deepcopy(scope)
        del missing['real_cloud_services']
        with self.assertRaises(ValueError):
            validate_scope(missing)
        for workers in (1, 10):
            altered = copy.deepcopy(scope)
            altered['fixture_configuration']['webdav']['http_workers'] = workers
            with self.subTest(workers=workers), self.assertRaises(ValueError):
                validate_scope(altered)
        altered = copy.deepcopy(scope)
        del altered['fixture_configuration']['webdav']['wsgi_serialization']
        with self.assertRaises(ValueError):
            validate_scope(altered)
        altered = copy.deepcopy(scope)
        del altered['fixture_configuration']
        with self.assertRaises(ValueError):
            validate_scope(altered)
        missing = copy.deepcopy(scope)
        del missing['provider_systems']['azure']
        with self.assertRaises(ValueError):
            validate_scope(missing)
