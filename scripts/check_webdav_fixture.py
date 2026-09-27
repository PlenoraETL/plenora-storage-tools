"""Check conditional creation on the public, isolated WebDAV fixture over HTTP.

This probe bypasses Storage Tools, so a server precondition race cannot be
mistaken for an adapter failure. It creates and deletes only unique owned keys.
"""
import argparse
import base64
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import os
from pathlib import Path
import subprocess
from threading import Barrier
import uuid

ROOT = Path(__file__).resolve().parents[1]


def validate_report(report, revision):
    if (report['status'] != 'PASS' or report['source_revision'] != revision or report['dirty']
            or report['workers'] != 8 or len(report['results']) != 30):
        raise ValueError('WebDAV fixture precondition proof is incomplete')
    for row in report['results']:
        if sorted(row['statuses']) != [201] + [412] * 7 or row['winner_preserved'] is not True:
            raise ValueError('WebDAV fixture accepted conflicting conditional writes')


def probe(report, save):
    host = os.environ.get('PLENORA_FIXTURE_HOST', 'webdav')
    port = int(os.environ.get('PLENORA_WEBDAV_PORT', 8088 if 'PLENORA_FIXTURE_HOST' in os.environ else 8080))
    authorization = 'Basic ' + base64.b64encode(b'plenora:plenora-fixture-secret').decode()

    def request(method, key, data=None, conditional=False):
        headers = {'Authorization': authorization}
        if conditional:
            headers['If-None-Match'] = '*'
        connection = http.client.HTTPConnection(host, port, timeout=30)
        try:
            connection.request(method, key, body=data, headers=headers)
            response = connection.getresponse()
            return response.status, response.read()
        finally:
            connection.close()

    for _ in range(30):
        key = '/conditional-probe-' + uuid.uuid4().hex
        payloads = [bytes([index]) * (65536 if index % 2 else 0) for index in range(8)]
        barrier = Barrier(8)

        def put(index):
            barrier.wait(timeout=30)
            return request('PUT', key, payloads[index], conditional=True)[0]

        try:
            with ThreadPoolExecutor(max_workers=8) as pool:
                statuses = list(pool.map(put, range(8)))
            winners = [index for index, status in enumerate(statuses) if status == 201]
            status, content = request('GET', key)
            preserved = len(winners) == 1 and status == 200 and content == payloads[winners[0]]
            report['results'].append({'statuses': statuses, 'winner_preserved': preserved})
            save()
        finally:
            # Keep cleanup failures visible without exposing the response body.
            status, _ = request('DELETE', key)
            if status not in (200, 204, 404):
                raise RuntimeError('WebDAV probe cleanup failed')
    if any(sorted(row['statuses']) != [201] + [412] * 7 or not row['winner_preserved']
           for row in report['results']):
        raise ValueError('WebDAV fixture violated conditional creation')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'target/release-readiness/webdav-fixture.json')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report = {'schema_version': 1, 'status': 'RUNNING', 'workers': 8, 'results': [],
              'source_revision': subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip(),
              'dirty': bool(subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()),
              'scope': 'Isolated WebDAV fixture; direct HTTP, not a Storage Tools binary test'}

    def save():
        args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')

    save()
    try:
        probe(report, save)
        report['status'] = 'PASS'
    except BaseException as error:
        report.update(status='FAIL', failure_type=type(error).__name__)
        # Socket exceptions may contain fixture endpoints: publish only type.
        raise RuntimeError('WebDAV fixture conditional creation check failed') from None
    finally:
        save()
    print('PASS WebDAV fixture: 30 rounds, 8 concurrent writers, one preserved winner')


if __name__ == '__main__':
    main()
