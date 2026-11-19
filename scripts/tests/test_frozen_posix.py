"""Real POSIX observations; Docker is only a fixture double, never a server claim."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_campaign_fence as existing
import campaign_fence as fence
import fixture_tls

TLS_PROFILE = {'ca.key': 0o600, 'ca.crt': 0o644, 'ca.srl': 0o644,
               'minio': 0o755, 'minio/private.key': 0o600, 'minio/public.crt': 0o644,
               'minio.csr': 0o644, 'minio.ext': 0o644,
               'extended': 0o755, 'extended/server.key': 0o600, 'extended/server.crt': 0o644}


@unittest.skipUnless(sys.platform == 'linux', 'requires Linux ACL and flock')
class FrozenPosixTests(existing.ProtocolCase):
    def assert_profile(self, root, profile, root_mode):
        actual = {p.relative_to(root).as_posix(): p.stat().st_mode & 0o777
                  for p in root.rglob('*')}
        self.assertEqual(actual, profile)
        self.assertEqual(root.stat().st_mode & 0o777, root_mode)
        for name, mode in {'.': root_mode, **profile}.items():
            with self.subTest(path=name):
                acl = subprocess.check_output(['getfacl', '--absolute-names', '--omit-header',
                                               str(root / name)], text=True)
                permissions = lambda shift: ''.join(c if (mode >> shift) & bit else '-'
                                                    for c, bit in (('r', 4), ('w', 2), ('x', 1)))
                self.assertEqual(acl.strip().splitlines(),
                                 ['user::' + permissions(6), 'group::' + permissions(3),
                                  'other::' + permissions(0)])

    def test_admission_inventory_and_reboot_lease(self):
        # A recorded boot different from this boot must use a bounded lease,
        # not an uptime from the previous boot (which could mean years).
        for mask in (0o000, 0o002, 0o022, 0o077):
            with self.subTest(umask=oct(mask)):
                if self.directory.exists():
                    shutil.rmtree(self.directory)
                previous = os.umask(mask)
                try:
                    controller, epoch = self.admitted()
                    self.assertEqual(self.refused(), fence.LOCK_HELD)
                    self.assertEqual(self.epoch(), epoch + '\n')
                    self.end(controller)
                    self.assert_profile(self.vm_root, {'.campaign': 0o700, '.campaign/lock': 0o600,
                                                       '.campaign/epoch': 0o600, '.campaign/lease': 0o600}, 0o700)
                    (self.directory / 'lease').write_text('00000000-0000-0000-0000-000000000000 999999999999\n')
                    # Bounded parent timeout also catches an accidental cross-boot sleep.
                    result = subprocess.run(['sh', '-c', fence.admission_command(str(self.vm_root), lease=0)],
                                            input='', capture_output=True, text=True,
                                            env=self.environment())
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertTrue(result.stdout.startswith('locked 2-'))
                finally:
                    os.umask(previous)

    def test_tls_profiles_enumerate_every_path_under_all_umasks(self):
        for mask in (0o000, 0o002, 0o022, 0o077):
            with self.subTest(umask=oct(mask)), tempfile.TemporaryDirectory() as temp:
                previous = os.umask(mask)
                try:
                    root = Path(temp) / 'tls'
                    fixture_tls.generate('minio', root)
                    fixture_tls.generate('ftps', root / 'extended')
                    self.assert_profile(root, TLS_PROFILE, 0o755)
                    self.assertEqual(os.umask(mask), mask)
                finally:
                    os.umask(previous)

    def test_preparation_profiles_enumerate_every_created_file(self):
        fixture = existing.PreparationProtocolTests()
        fixture.setUp()
        self.addCleanup(fixture.tearDown)
        fixture.install_real_preparation()
        expected = dict(TLS_PROFILE, **{
            'certificate-generation.log': 0o644, 'extended/certificate-generation.log': 0o644,
            'sftp-fingerprint': 0o644, 'sftp-client': 0o600, 'sftp-client.pub': 0o644,
            'sftp-client-encrypted': 0o600, 'sftp-client-encrypted.pub': 0o644})
        root = fixture.checkout / '.fixtures'
        for mask in (0o000, 0o002, 0o022, 0o077):
            with self.subTest(umask=oct(mask)):
                shutil.rmtree(root)
                for script in ('prepare-sftp-keys.sh', 'prepare-fixtures.sh', 'prepare-extended-fixtures.sh'):
                    result = subprocess.run(['bash', '-c', f'umask {mask:03o}; bash scripts/{script}'],
                                            cwd=fixture.checkout, env=fixture.environment(),
                                            capture_output=True)
                    self.assertEqual(result.returncode, 0)
                self.assert_profile(root, expected, 0o755)

    def test_real_access_and_default_acl_refuse_before_creation(self):
        self.assertIsNotNone(existing.REAL_GETFACL, 'real getfacl required')
        self.assertIsNotNone(shutil.which('setfacl'), 'real setfacl required')
        for acl in ('u:65534:rwx', 'd:u:65534:rwx'):
            with self.subTest(acl=acl), tempfile.TemporaryDirectory() as temp:
                top = Path(temp)
                root = top / 'private'
                root.mkdir(mode=0o700)
                clean = subprocess.run(fence.private_root_command(str(root), str(top)),
                                       shell=True, capture_output=True)
                self.assertEqual(clean.returncode, 0)
                subprocess.run(['setfacl', '-m', acl, str(root)], check=True, capture_output=True)
                before = subprocess.check_output(['getfacl', '-cp', str(root)])
                refused = subprocess.run(fence.private_root_command(str(root / 'new'), str(top)),
                                         shell=True, capture_output=True)
                self.assertNotEqual(refused.returncode, 0)
                self.assertFalse((root / 'new').exists())
                self.assertEqual(subprocess.check_output(['getfacl', '-cp', str(root)]), before)


if __name__ == '__main__':
    unittest.main()
