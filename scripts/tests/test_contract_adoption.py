"""Adoption must fail closed without disclosing mutated document data."""
import contextlib
import io
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_contract_adoption as gate


class AdoptionTests(unittest.TestCase):
    def test_checked_in_policy_and_vendor_inventory(self):
        with contextlib.redirect_stdout(io.StringIO()):
            gate.check()

    def test_changed_vendor_bytes_fail_with_no_payload_in_diagnostic(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copytree(gate.ROOT / 'contracts', root / 'contracts')
            vendor = root / 'contracts/upstream'
            (vendor / 'storage-tools-v1.json').write_text('PRIVATE_SENTINEL')
            with patch.object(gate, 'ROOT', root), patch.object(gate, 'UPSTREAM', vendor):
                with patch.object(sys, 'argv', ['check_contract_adoption.py']):
                    with self.assertRaises(SystemExit) as failure:
                        gate.main()
            self.assertEqual(str(failure.exception),
                             'FAIL adoption gate: invalid input or inconsistent evidence')

    def test_unreadable_input_does_not_expose_exception(self):
        with patch.object(gate, 'check', side_effect=OSError('PRIVATE_SENTINEL')):
            with patch.object(sys, 'argv', ['check_contract_adoption.py']):
                with self.assertRaises(SystemExit) as failure:
                    gate.main()
        self.assertNotIn('PRIVATE_SENTINEL', str(failure.exception))
