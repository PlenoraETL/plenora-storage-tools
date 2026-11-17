"""Cross-platform byte identity and early campaign rejection regressions."""
import gzip
from contextlib import contextmanager
import io
import os
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import shlex
import shutil
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from check_checkout_bytes import check
from package_source import content_digest
import release_publication as publication
import campaign_shell as shell
import release_campaign as controller


class PublicationBytesTests(unittest.TestCase):
    def test_shared_source_compares_content_and_preserves_selected_gzip(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            version = '4.0.0'
            def archive(target, payload, level, reverse=False, mode=0o644):
                path = root / target / f'plenora-storage-{version}-source.tar.gz'
                path.parent.mkdir(exist_ok=True)
                raw = io.BytesIO()
                with tarfile.open(fileobj=raw, mode='w', format=tarfile.PAX_FORMAT) as tar:
                    entries = [('Cargo.toml', payload), ('README.md', b'source\n')]
                    for name, data in reversed(entries) if reverse else entries:
                        member = tarfile.TarInfo(f'plenora-storage-{version}/' + name)
                        member.size, member.mode, member.mtime = len(data), mode, 0
                        tar.addfile(member, io.BytesIO(data))
                path.write_bytes(gzip.compress(raw.getvalue(), compresslevel=level, mtime=0))
                return path
            linux = archive(publication.TARGETS[0], b'same\n', 1)
            windows = archive(publication.TARGETS[1], b'same\n', 9)
            self.assertNotEqual(publication.digest(linux), publication.digest(windows))
            self.assertEqual(gzip.decompress(linux.read_bytes()), gzip.decompress(windows.read_bytes()))
            before = linux.read_bytes()
            self.assertEqual(publication.shared_source(root, version), linux)
            self.assertEqual(linux.read_bytes(), before)
            self.assertEqual(content_digest(linux), content_digest(windows))
            # Every tar field is source content: payload, member order and mode.
            for changed in ({'payload': b'different\n'}, {'reverse': True}, {'mode': 0o755}):
                with self.subTest(changed=changed):
                    options = {'payload': b'same\n', 'level': 9, **changed}
                    archive(publication.TARGETS[1], **options)
                    with self.assertRaisesRegex(ValueError, 'source contents differ'):
                        publication.shared_source(root, version)
            archive(publication.TARGETS[1], b'same\n', 9)
            self.assertEqual(publication.shared_source(root, version), linux)

    def test_all_seeds_survive_autocrlf_and_preflight_rejects_changed_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
            def git(*args):
                return subprocess.check_output(['git', *args], cwd=root, env=env, stderr=subprocess.PIPE)
            git('init', '-q')
            source = Path(__file__).resolve().parents[2]
            (root / '.gitattributes').write_bytes((source / '.gitattributes').read_bytes())
            seeds = list((source / 'fuzz/seeds').rglob('*'))
            expected = {}
            for path in seeds:
                if path.is_file():
                    name = path.relative_to(source)
                    (root / name).parent.mkdir(parents=True, exist_ok=True)
                    expected[name] = path.read_bytes()
                    (root / name).write_bytes(expected[name])
            # Also cover files outside the release source digest's limited scope.
            (root / 'extra.txt').write_bytes(b'blob\n')
            git('-c', 'core.autocrlf=false', 'add', '.')
            tree = git('write-tree').decode().strip()
            git('-c', 'core.autocrlf=true', 'checkout-index', '--all', '--force')
            for name, data in expected.items():
                self.assertEqual((root / name).read_bytes(), data)
            (root / 'extra.txt').write_bytes(b'blob\n')
            check(root, tree)
            name = next(n for n, data in expected.items() if b'\n' in data and b'\r\n' not in data)
            (root / name).write_bytes(expected[name].replace(b'\n', b'\r\n'))
            with self.assertRaisesRegex(ValueError, 'working bytes differ'):
                check(root, tree)
            (root / name).write_bytes(expected[name])
            (root / 'extra.txt').write_bytes(b'changed\n')
            with self.assertRaisesRegex(ValueError, 'working bytes differ'):
                check(root, tree)

    def test_preflight_precedes_workflows_and_remote_access(self):
        with patch.object(controller.sys, 'platform', 'win32'), \
                patch.object(controller, 'configuration', return_value={}), \
                patch.object(controller.subprocess, 'check_output', side_effect=['a' * 40, '']), \
                patch.object(controller, 'check_checkout_bytes', side_effect=ValueError('byte mismatch')), \
                patch.object(controller, 'Remote') as remote, patch.object(controller, 'Campaign') as campaign:
            with self.assertRaisesRegex(ValueError, 'byte mismatch'):
                controller.run(Path('config'), Path('output'), [], None)
            remote.assert_not_called()
            campaign.assert_not_called()

    def test_checkout_git_and_teardown_commands_are_confined(self):
        status = shell.checkout_status_command('/fixture/source')
        self.assertLess(status.index('umask 077'), status.index('git status'))
        command = shell.teardown_command('/fixture/source', 'storage-q-test', '/fixture/.campaign', '1-' + 'a' * 32)
        self.assertIn('docker compose -p storage-q-test -f docker-compose.yml -f compose.extended.yml down --remove-orphans', command)
        self.assertNotIn('prune', command)
        self.assertNotIn('--volumes', command)
        self.assertLess(command.index('flock'), command.index('docker compose'))

    def test_cleanup_precedes_admission_release_on_success_and_failure(self):
        for failed in (None, 'prepare-vm', 'qualify-windows', 'qualify-vm', 'seal'):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                config = root / 'config.json'
                config.write_text('{}')
                events = []
                remote = Mock()
                remote.run.side_effect = lambda command: events.append(command)
                session = Mock(directory='/fixture/.campaign', epoch='1-' + 'a' * 32)
                @contextmanager
                def admitted(*args):
                    try:
                        yield session
                    finally:
                        events.append('release admission')
                campaign = Mock()
                def phase(name, action, **kwargs):
                    if name == failed:
                        raise ValueError('synthetic phase failure')
                    return root
                campaign.phase.side_effect = phase
                with patch.object(controller.sys, 'platform', 'win32'), \
                        patch.object(controller, 'configuration', return_value={'vm_root': '/fixture', 'host': 'fixture.invalid'}), \
                        patch.object(controller.subprocess, 'check_output', side_effect=['a' * 40, '']), \
                        patch.object(controller, 'check_checkout_bytes'), \
                        patch.object(controller, 'validate_transport_resume'), \
                        patch.object(controller, 'Campaign', return_value=campaign), \
                        patch.object(controller, 'Remote', return_value=remote), \
                        patch.object(controller, 'check_private_root'), \
                        patch.object(controller, 'admission', admitted):
                    if failed:
                        with self.assertRaises(Exception):
                            controller.run(config, root / 'out', [], None)
                    else:
                        controller.run(config, root / 'out', [], None)
                cleanup = [i for i, command in enumerate(events) if 'down --remove-orphans' in command]
                self.assertEqual(len(cleanup), 1)
                self.assertLess(cleanup[0], events.index('release admission'))
                remote.client.close.assert_called_once()

    def test_teardown_shell_executes_down_and_refuses_a_new_epoch(self):
        bash = shutil.which('bash')
        self.assertIsNotNone(bash)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ('lock', 'docker-compose.yml', 'compose.extended.yml'):
                (root / name).touch()
            epoch = '1-' + 'a' * 32
            (root / 'epoch').write_text(epoch + '\n', newline='\n')
            command = shell.teardown_command(root.as_posix(), 'storage-q-test', root.as_posix(), epoch)
            # Observe the real generated shell argv; no Docker daemon needed.
            prefix = 'flock() { return 0; }; docker() { printf "<%s>" "$@"; }; export -f flock docker; '
            result = subprocess.run([bash, '-c', prefix + command], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, '<compose><-p><storage-q-test><-f><docker-compose.yml><-f><compose.extended.yml><down><--remove-orphans>')
            (root / 'epoch').write_text('2-' + 'b' * 32 + '\n', newline='\n')
            result = subprocess.run([bash, '-c', prefix + command], capture_output=True, text=True)
            self.assertEqual(result.returncode, 77, result.stderr)
            self.assertNotIn('<compose>', result.stdout)

    @unittest.skipUnless(os.name == 'posix', 'real POSIX index permissions')
    def test_git_status_does_not_create_group_writable_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            env = dict(os.environ, GIT_CONFIG_GLOBAL=os.devnull, GIT_CONFIG_NOSYSTEM='1')
            subprocess.run(['git', 'init', '-q', str(root)], env=env, check=True)
            (root / 'file').write_text('data')
            subprocess.run(['git', '-C', str(root), 'add', '.'], env=env, check=True)
            # Force an index refresh under a hostile ambient umask.
            os.utime(root / 'file', (1, 1))
            subprocess.run(['bash', '-c', 'umask 0002; ' + shell.checkout_status_command(str(root))],
                           env=env, capture_output=True)
            self.assertEqual((root / '.git/index').stat().st_mode & 0o022, 0)
