"""Real shell execution of the CI-to-verifier boundary, with Python argv probes."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from frozen_plan import workflows
from frozen_proofs import detail

ROOT = Path(__file__).resolve().parents[2]
PROBE = '''import hashlib,json,os,pathlib,sys
args=sys.argv[1:]
subject=pathlib.Path(os.environ['FROZEN_SUBJECT']).read_bytes()
with open(os.environ['FROZEN_TRACE'],'a',encoding='utf-8') as stream:
    stream.write(json.dumps({'argv':args,'subject':hashlib.sha256(subject).hexdigest()})+'\\n')
if args == ['scripts/release_version.py']: print('4.0.0')
'''


class ShellPlanTests(unittest.TestCase):
    def test_ci_shell_reaches_verifier_with_exact_subject_and_rejects_unreachable_command(self):
        windows = sys.platform == 'win32'
        job = 'native-windows' if windows else 'release-package-recheck'
        label = 'Verify the Windows candidate artifacts' if windows else 'Exercise candidate and publication guards on the built bytes'
        step = next(s for s in workflows(ROOT)['ci.yml']['jobs'][job]['steps'] if s.get('name') == label)
        body = step['run']
        verifier = 'scripts/qualify_windows_candidate.py' if windows else 'scripts/check_release_packages.py'
        expected = ['dist/4.0.0'] if windows else ['target/candidate-recheck/4.0.0', '--release']
        def observe(source):
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp)
                probe, script, trace, subject = (root / n for n in ('probe.py', 'step', 'trace.jsonl', 'subject.bin'))
                probe.write_text(PROBE, encoding='utf-8')
                subject.write_bytes(b'exact synthetic candidate')
                env = dict(os.environ, FROZEN_PYTHON=sys.executable, FROZEN_PROBE=str(probe),
                           FROZEN_TRACE=str(trace), FROZEN_SUBJECT=str(subject), PATH=str(root))
                if windows:
                    shell = shutil.which('pwsh') or shutil.which('powershell')
                    self.assertIsNotNone(shell)
                    prefix = 'function python { & $env:FROZEN_PYTHON $env:FROZEN_PROBE @args; $global:LASTEXITCODE = $LASTEXITCODE }\n'
                    script = script.with_suffix('.ps1')
                    command = [shell, '-NoProfile', '-NonInteractive', '-Command', prefix + source]
                else:
                    shell = shutil.which('bash')
                    self.assertIsNotNone(shell)
                    prefix = 'python() { "$FROZEN_PYTHON" "$FROZEN_PROBE" "$@"; }\n'
                    command = [shell, '-euo', 'pipefail', str(script)]
                script.write_text(prefix + source, encoding='utf-8')
                result = subprocess.run(command, cwd=root, env=env, capture_output=True)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors='replace'))
                records = [json.loads(line) for line in trace.read_text().splitlines()] if trace.exists() else []
                self.assertEqual([r['argv'] for r in records], [['scripts/release_version.py'], [verifier, *expected]])
                self.assertEqual({r['subject'] for r in records}, {hashlib.sha256(subject.read_bytes()).hexdigest()})
                detail('ci-shell-observed.json', {'job': job, 'step': label, 'scope': 'argv-probe', 'records': records})
        observe(body)
        # Temporary mutation of the parsed step, retaining the verifier text in
        # a branch which is never reached. The independent observer must fail.
        hidden = 'if ($false) {\n' + body + '\n}' if windows else 'if false; then\n' + body + '\nfi'
        with self.assertRaises(AssertionError):
            observe(hidden)


if __name__ == '__main__':
    unittest.main()
