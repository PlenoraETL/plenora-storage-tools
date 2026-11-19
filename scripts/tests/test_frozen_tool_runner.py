"""Real subprocess smoke of the CI command wrapper, with no Rust toolchain."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from frozen_proofs import detail


class ToolRunnerTests(unittest.TestCase):
    def test_real_wrapper_preserves_argv_environment_log_and_failure_code(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            probe = root / 'probe.py'
            probe.write_text("import json,os,sys\nprint(json.dumps({'argv':sys.argv[2:],"
                             "'color':os.environ['CARGO_TERM_COLOR'],'no_color':os.environ['NO_COLOR']}))\n"
                             "sys.exit(int(sys.argv[1]))\n")
            observations = []
            for code in (0, 5):
                log = root / f'{code}.log'
                args = ['a value with spaces', '--literal=$not_expanded', 'unicode-è']
                command = [sys.executable, str(ROOT / 'scripts/run_logged.py'), str(log),
                           sys.executable, str(probe), str(code), *args]
                result = subprocess.run(command, env=dict(os.environ, CARGO_TERM_COLOR='always'),
                                        capture_output=True)
                self.assertEqual(result.returncode, code)
                observed = json.loads(log.read_text())
                self.assertEqual(observed, {'argv': args, 'color': 'never', 'no_color': '1'})
                observations.append({'scope': 'python-tool-smoke', 'code': result.returncode, 'observed': observed})
            detail('tool-smoke.json', observations)


if __name__ == '__main__':
    unittest.main()
