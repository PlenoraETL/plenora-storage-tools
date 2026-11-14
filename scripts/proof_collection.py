"""Collect independent checks; never convert a failed prerequisite into PASS."""
from contextlib import contextmanager
import json


class Collection:
    def __init__(self, fatal=()):
        self.results = []
        self.fatal = fatal

    @contextmanager
    def case(self, name):
        row = {'name': name, 'status': 'RUNNING'}
        self.results.append(row)
        try:
            yield
        except Exception as error:
            # Callback and subprocess messages may contain private material.
            row.update(status='FAIL', failure_type=type(error).__name__)
            if isinstance(error, self.fatal):
                raise
        else:
            row['status'] = 'PASS'

    def finish(self, output=None):
        passed = bool(self.results) and all(r['status'] == 'PASS' for r in self.results)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps({'status': 'PASS' if passed else 'FAIL',
                                          'results': self.results}, indent=2) + '\n', encoding='utf-8')
        if not passed:
            raise RuntimeError('qualification checks failed; inspect the collection report')
