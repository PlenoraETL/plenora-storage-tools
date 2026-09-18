"""Qualify ambiguous multipart commits against disposable MinIO fixtures.

A local HTTP relay withholds the completion request or its successful response.
Only fixture objects under a unique prefix are created and removed.
"""
import datetime
import hashlib
import http.client
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import tempfile
import threading
import urllib.parse
import uuid

from qualify_cli import BINARY, ROOT, invoke


def qualify(mode, interruption, work):
    upstream = urllib.parse.urlsplit(os.environ['PLENORA_MINIO_ENDPOINT'])
    assert upstream.scheme == 'http', 'fault relay requires the isolated HTTP fixture'
    ready, release, finished = threading.Event(), threading.Event(), threading.Event()
    events, failures = [], []

    class Relay(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def forward(self):
            completing = False
            try:
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
                completing = self.command == 'POST' and 'uploadId' in query
                assert 'chunked' not in self.headers.get('Transfer-Encoding', '').lower()
                length = int(self.headers.get('Content-Length', 0))
                assert length <= 16 * 1024 * 1024
                body = self.rfile.read(length)
                if completing and mode == 'before_commit':
                    ready.set()
                    assert release.wait(30), 'test did not release pending commit'
                connection = http.client.HTTPConnection(upstream.hostname, upstream.port or 80, timeout=10)
                connection.request(self.command, self.path, body, dict(self.headers))
                response = connection.getresponse()
                response_body = response.read()
                events.append((self.command, 'partNumber' in query, completing, response.status))
                if completing and mode == 'after_commit':
                    assert response.status == 200, response.status
                    ready.set()
                    assert release.wait(30), 'test did not release lost response'
                try:
                    self.send_response(response.status)
                    for name, value in response.getheaders():
                        if name.lower() not in ['transfer-encoding', 'content-length', 'connection']:
                            self.send_header(name, value)
                    self.send_header('Content-Length', str(len(response_body)))
                    self.end_headers()
                    if self.command != 'HEAD':
                        self.wfile.write(response_body)
                except (BrokenPipeError, ConnectionResetError):
                    pass  # The client has already returned its typed failure.
                finally:
                    connection.close()
            except Exception as error:
                failures.append(type(error).__name__ + ': ' + str(error))
                ready.set()
            finally:
                if completing:
                    finished.set()

        do_GET = do_HEAD = do_PUT = do_POST = do_DELETE = forward

    key = 'commit-faults/' + uuid.uuid4().hex
    payload = bytes(range(256)) * 32768 + b'commit-fault'
    source = work / 'payload.bin'
    source.write_bytes(payload)
    config = json.loads((ROOT / 'docker/minio-connection.json').read_text())
    config['config']['endpoint'] = os.environ['PLENORA_MINIO_ENDPOINT']
    direct = work / 'direct.json'
    direct.write_text(json.dumps(config))
    flags = ['--allow-insecure-http']
    with http.server.ThreadingHTTPServer(('127.0.0.1', 0), Relay) as relay:
        thread = threading.Thread(target=relay.serve_forever, daemon=True)
        thread.start()
        config['config']['endpoint'] = f'http://127.0.0.1:{relay.server_port}'
        proxied = work / 'relay.json'
        proxied.write_text(json.dumps(config))
        args = [str(BINARY), '--format', 'json',
                '--allow-private-network', *flags]
        if interruption == 'deadline':
            deadline = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=5)
            args += ['--deadline', deadline.isoformat()]
        args += ['put', '--connection', str(proxied), '--key', key, '--input', str(source),
                 '--overwrite', 'true', '--publication-policy', 'atomic-required']
        process = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            assert ready.wait(12), 'completion phase was not reached'
            assert not failures, failures
            assert any(part and status == 200 for _, part, _, status in events), events
            # Observe actual backend state independently while the response is withheld.
            if mode == 'before_commit':
                result = invoke(direct, flags, 'stat', '--key', key, expected=5)
                assert result['category'] == 'not_found', result
            else:
                assert invoke(direct, flags, 'stat', '--key', key)['size'] == len(payload)
            if interruption == 'sigterm':
                process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=12)
            assert not stderr and len(stdout.splitlines()) == 1, (stdout, stderr)
            envelope = json.loads(stdout)
            error = envelope['error']
            assert process.returncode == (5 if interruption == 'deadline' else 130), envelope
            assert error['category'] == ('timeout' if interruption == 'deadline' else 'cancelled'), error
            assert error['phase'] == 'commit' and error['remote_effect'] == 'unknown', error
            assert error['retry']['kind'] == 'requires_recovery', error
        finally:
            if process.poll() is None:
                process.kill()
            process.communicate()
            release.set()
            assert finished.wait(12), 'backend completion did not finish'
            relay.shutdown()
            thread.join()
    assert not failures, failures
    assert any(complete and status == 200 for _, _, complete, status in events), events
    # A server may finish a request after the caller stops waiting. Reconcile
    # by reading and hashing the object before any retry or compensating delete.
    output = work / f'{mode}-{interruption}.bin'
    invoke(direct, flags, 'get', '--key', key, '--output', str(output), '--overwrite', 'false')
    assert output.read_bytes() == payload
    invoke(direct, flags, 'delete', '--key', key, '--ignore-missing', 'false')
    return {'mode': mode, 'interruption': interruption, 'status': 'PASS',
            'reported_effect': 'unknown', 'recovery': 'verified published bytes then removed owned object'}


def main():
    assert os.name == 'posix', 'SIGTERM qualification runs in the Linux fixture runner'
    with tempfile.TemporaryDirectory(prefix='storage-commit-faults-') as directory:
        results = [qualify(mode, interruption, Path(directory))
                   for mode in ['before_commit', 'after_commit']
                   for interruption in ['deadline', 'sigterm']]
    print(json.dumps({'binary_sha256': hashlib.sha256(BINARY.read_bytes()).hexdigest(), 'results': results}))


if __name__ == '__main__':
    main()
