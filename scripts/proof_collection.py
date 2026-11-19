"""Collect independent checks; never convert a failed prerequisite into PASS."""
from contextlib import contextmanager
import sys
import shutil
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
            # The report may live where the caller cannot read it (a container
            # filesystem): name the failed cases in the log too. Case names are
            # code literals and failure_type an exception class, never data.
            for row in self.results:
                if row['status'] != 'PASS':
                    print(f"FAIL case {row['name']}: {row.get('failure_type', 'not completed')}",
                          file=sys.stderr, flush=True)
            raise RuntimeError('qualification checks failed; inspect the collection report')


@contextmanager
def export_reports(reports, *, fresh=True):
    """Export freshly produced reports even when the producer fails.

    Explicit (source, destination) pairs prevent accidental temporary-file export.
    Remove old sources before execution so a failed attempt cannot reuse PASS.
    """
    reports = list(reports)
    if fresh:
        for source, destination in reports:
            source.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
    try:
        yield
    finally:
        for source, destination in reports:
            if source.is_file():
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, destination)
