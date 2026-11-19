"""Keep automatic checks bounded and platform support explicit."""
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from ci_plan import workflows
from workflow_policy import check, automatic_jobs

ROOT = Path(__file__).resolve().parents[2]


class WorkflowPolicyTests(unittest.TestCase):
    def test_current_workflows(self):
        check(workflows(ROOT))

    def test_manual_condition_cannot_be_removed(self):
        documents = workflows(ROOT)
        del documents['ci.yml']['jobs']['docker-conformance']['if']
        with self.assertRaisesRegex(ValueError, 'manual'):
            check(documents)

    def test_schedule_cannot_start_exploration(self):
        documents = workflows(ROOT)
        del documents['scheduled.yml']['jobs']['parser-fuzz']['if']
        with self.assertRaisesRegex(ValueError, 'manual'):
            check(documents)

    def test_reusable_timeout_is_checked(self):
        documents = workflows(ROOT)
        documents['dependencies.yml']['jobs']['dependencies']['timeout-minutes'] = 60
        with self.assertRaisesRegex(ValueError, '30 minutes'):
            check(documents)

    def test_unsupported_runner_is_rejected(self):
        documents = workflows(ROOT)
        documents['ci.yml']['jobs']['rust-checks']['strategy']['matrix']['os'].append('unsupported-runner')
        with self.assertRaisesRegex(ValueError, 'unsupported platform'):
            check(documents)

    def test_pr_local_plan_excludes_manual_callers_and_children(self):
        from ci_locale import derive
        jobs = derive(ROOT)['jobs']
        selected = {job['id'] for job in jobs if job['selected']}
        self.assertIn('rust-checks', selected)
        self.assertIn('dependencies', selected)
        self.assertIn('production-coverage', selected)
        self.assertIn('rust-product', selected)
        self.assertNotIn('native-windows', selected)

    def test_manual_jobs_remain_available(self):
        documents = workflows(ROOT)
        self.assertIn('workflow_dispatch', documents['ci.yml']['on'])
        automatic = set(automatic_jobs(documents))
        self.assertIn(('ci.yml', 'rust-checks'), automatic)
        self.assertIn(('coverage.yml', 'rust-product'), automatic)

    def test_independent_libraries_reject_other_platforms(self):
        for crate in ('plenora-storage-core', 'plenora-smb2'):
            source = (ROOT / 'crates' / crate / 'src/lib.rs').read_text(encoding='utf-8')
            self.assertIn('#[cfg(not(any(target_os = "linux", target_os = "windows")))]\n'
                          'compile_error!("Plenora Storage supports only Linux and Windows");', source)

    def test_short_jobs_cannot_be_made_manual_or_depend_on_long_jobs(self):
        from workflow_policy import AUTOMATIC, MANUAL
        for key in AUTOMATIC:
            for mutation in ('condition', 'dependency'):
                documents = workflows(ROOT)
                job = documents['ci.yml']['jobs'][key]
                if mutation == 'condition':
                    job['if'] = MANUAL
                else:
                    job['needs'] = ['native-windows']
                with self.subTest(job=key, mutation=mutation), self.assertRaises(ValueError):
                    check(documents)

    def test_required_check_names(self):
        documents = workflows(ROOT)
        for workflow, key in [('ci.yml', 'product-quality'),
                              ('ci.yml', 'dependencies'), ('dependencies.yml', 'dependencies'),
                              ('ci.yml', 'api-compatibility'), ('api-compatibility.yml', 'rust-api'),
                              ('ci.yml', 'parser-fuzz'), ('parser-fuzz.yml', 'parsers')]:
            self.assertNotIn('name', documents[workflow]['jobs'][key])
        self.assertEqual(documents['api-compatibility.yml']['jobs']['rust-api']['strategy']['matrix']['os'],
                         ['ubuntu-24.04', 'windows-latest'])
