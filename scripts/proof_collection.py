"""Collect independent checks; never convert a failed prerequisite into PASS."""
from contextlib import contextmanager
from campaign_model import write_json, transition


class Collection:
    def __init__(self, fatal=()):
        self.results = []
        self.fatal = fatal

    @contextmanager
    def case(self, name):
        row = {'name': name, 'status': transition('phase', 'NEW', 'start')}
        self.results.append(row)
        try:
            yield
        except Exception as error:
            # Callback and subprocess messages may contain private material.
            row.update(status=transition('phase', row['status'], 'failure'), failure_type=type(error).__name__)
            if isinstance(error, self.fatal):
                raise
        else:
            row['status'] = transition('phase', row['status'], 'success')

    def finish(self, output=None):
        passed = bool(self.results) and all(r['status'] == 'PASS' for r in self.results)
        if output is not None:
            output.parent.mkdir(parents=True, exist_ok=True)
            write_json(output, {'status': 'PASS' if passed else 'FAIL', 'results': self.results})
        if not passed:
            raise RuntimeError('qualification checks failed; inspect the collection report')
