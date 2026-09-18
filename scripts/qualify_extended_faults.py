"""Deterministic protocol-failure checks; only disposable localhost servers."""
import datetime
import hashlib
import http.server
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import time

ROOT = Path(__file__).resolve().parents[1]
BINARY = Path(os.environ.get('PLENORA_CLI_BIN', ROOT / 'target/debug' / ('plenora-storage.exe' if os.name == 'nt' else 'plenora-storage'))).resolve()
SECRET = 'extended-fault-secret'
EXPECTED_TESTS = {
    'azure_redirect_blocked', 'gcs_redirect_blocked', 'webdav_redirect_blocked',
    'azure_raw_key_rejected', 'gcs_raw_key_rejected', 'gcs_repeated_page_token_rejected',
    'webdav_absent_optional_property', 'webdav_outside_root_rejected',
    'webdav_partial_multistatus_not_success', 'webdav_commit_deadline_unknown',
}
ENV = dict(os.environ, PLENORA_FAULT_CREDENTIALS=json.dumps({'bearer_token': SECRET}))


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def respond(self):
        self.server.calls += 1
        length = int(self.headers.get('Content-Length', 0))
        if length:
            self.rfile.read(length)
        if self.server.delay:
            time.sleep(self.server.delay)
        self.send_response(self.server.status)
        self.send_header('Content-Length', str(len(self.server.body)))
        if self.server.redirect:
            self.send_header('Location', self.server.redirect)
        self.end_headers()
        try:
            self.wfile.write(self.server.body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            pass

    do_GET = respond
    do_PUT = respond
    do_POST = respond
    do_PROPFIND = respond
    do_DELETE = respond


def main():
    results = []
    with tempfile.TemporaryDirectory(prefix='storage-faults-') as temporary, \
            http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as server, \
            http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler) as redirect_target:
        directory = Path(temporary)
        for fixture in [server, redirect_target]:
            fixture.status, fixture.body, fixture.calls, fixture.delay, fixture.redirect = 200, b'', 0, 0, None
            threading.Thread(target=fixture.serve_forever, daemon=True).start()
        endpoint = f'http://127.0.0.1:{server.server_port}'
        source = directory / 'payload'
        source.write_bytes(b'payload')

        def invoke(provider, operation='list', args=(), flags=(), success=False):
            config = {'endpoint': endpoint}
            if provider == 'azure':
                config.update(account='fixture', container='objects')
            elif provider == 'gcs':
                config.update(bucket='objects')
            else:
                config['endpoint'] += '/'
            connection = directory / 'connection.json'
            connection.write_text(json.dumps({'provider': provider, 'config_contract': f'plenora-storage-{provider}-connection-v1',
                                              'credential_ref': 'env:PLENORA_FAULT_CREDENTIALS', 'config': config}))
            command = [str(BINARY), '--format', 'json', '--allow-private-network', '--allow-insecure-http', *flags,
                       operation, '--connection', str(connection), *args]
            process = subprocess.run(command, capture_output=True, text=True, env=ENV, timeout=15)
            assert not process.stderr and len(process.stdout.splitlines()) == 1, process
            assert SECRET not in process.stdout, 'credentials leaked'
            value = json.loads(process.stdout)
            assert (process.returncode == 0) == success, value
            return value

        def passed(name):
            results.append({'name': name, 'status': 'PASS'})

        server.status = 307
        server.redirect = f'http://127.0.0.1:{redirect_target.server_port}/stolen'
        for provider in ['azure', 'gcs', 'webdav']:
            invoke(provider)
            assert redirect_target.calls == 0, 'redirect followed'
            passed(provider + '_redirect_blocked')
        server.status, server.redirect = 200, None
        server.body = b'<EnumerationResults><Blobs><Blob><Name>folder/</Name></Blob></Blobs></EnumerationResults>'
        invoke('azure')
        passed('azure_raw_key_rejected')
        server.body = b'{"items":[{"name":"folder/","size":"1","generation":"1"}]}'
        invoke('gcs')
        passed('gcs_raw_key_rejected')
        server.body = b'{"items":[],"nextPageToken":"repeated"}'
        before = server.calls
        invoke('gcs')
        assert server.calls - before <= 3, 'unbounded continuation loop'
        passed('gcs_repeated_page_token_rejected')
        server.status = 207
        server.body = b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>/</d:href><d:propstat><d:prop><d:resourcetype><d:collection/></d:resourcetype></d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat><d:propstat><d:prop><d:getcontentlength/></d:prop><d:status>HTTP/1.1 404 Not Found</d:status></d:propstat></d:response></d:multistatus>'
        invoke('webdav', 'test', success=True)
        passed('webdav_absent_optional_property')
        server.body = b'<d:multistatus xmlns:d="DAV:"><d:response><d:href>http://elsewhere.invalid/file</d:href></d:response></d:multistatus>'
        invoke('webdav')
        passed('webdav_outside_root_rejected')
        server.body = b'<d:multistatus xmlns:d="DAV:"/>'
        args = ('--key', 'payload', '--input', str(source), '--overwrite', 'true', '--publication-policy', 'best-effort')
        error = invoke('webdav', 'put', args)['error']
        assert error['remote_effect'] == 'unknown', error
        passed('webdav_partial_multistatus_not_success')
        server.status, server.body, server.delay = 201, b'', 2
        deadline = (datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=1)).isoformat()
        error = invoke('webdav', 'put', args, ('--deadline', deadline))['error']
        assert error['category'] == 'timeout' and error['remote_effect'] == 'unknown', error
        passed('webdav_commit_deadline_unknown')
        server.shutdown()
        redirect_target.shutdown()
    report = {'binary_sha256': hashlib.sha256(BINARY.read_bytes()).hexdigest(), 'results': results}
    assert {result['name'] for result in results} == EXPECTED_TESTS
    output = ROOT / 'target/release-readiness/extended-regressions.json'
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report, indent=2))


if __name__ == '__main__':
    main()
