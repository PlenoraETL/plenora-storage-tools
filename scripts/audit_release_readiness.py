"""Black-box release regressions using disposable localhost servers.

Build the CLI first with `cargo build --locked -p plenora-storage-cli`.
Writes observations under target/release-readiness; never contacts real storage.
"""

import datetime
import hashlib
import http.server
import json
import os
import pathlib
import socketserver
import subprocess
import threading
import time
import urllib.parse

ROOT = pathlib.Path(__file__).resolve().parents[1]
WORK = ROOT / 'target/release-readiness'
WORK.mkdir(parents=True, exist_ok=True)
CLI = pathlib.Path(os.environ.get('PLENORA_CLI_BIN', str(ROOT / 'target/debug' / ('plenora-storage.exe' if os.name == 'nt' else 'plenora-storage'))))
ENV = dict(os.environ, PLENORA_AUDIT_CREDENTIALS=json.dumps({
    'access_key_id': 'audit', 'secret_access_key': 'audit',
    'username': 'audit', 'password': 'audit'}))
BASE = [str(CLI), '--format', 'json', '--allow-private-network']

def connection(provider, config):
    path = WORK / (provider + '.json')
    path.write_text(json.dumps({'provider': provider,
        'config_contract': f'plenora-storage-{provider}-connection-v1',
        'config': config, 'credential_ref': 'env:PLENORA_AUDIT_CREDENTIALS'}))
    return str(path)

class S3(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        query = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if 'list-type' not in query:
            body = b'qualified download'
            self.send_response(200)
            self.send_header('Content-Type', 'application/octet-stream')
            self.send_header('Last-Modified', 'Fri, 18 Sep 2026 00:00:00 GMT')
            self.send_header('ETag', '"audit"')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        offset = query.get('start-after', [''])[0]
        objects = ''.join(f'<Contents><Key>{key}</Key><LastModified>2026-09-18T00:00:00Z</LastModified><ETag>"audit"</ETag><Size>1</Size><StorageClass>STANDARD</StorageClass></Contents>' for key in ('a', 'b', 'c') if key > offset)
        body = ('<ListBucketResult xmlns="http://s3.amazonaws.com/doc/2006-03-01/"><Name>audit</Name><Prefix></Prefix><KeyCount>2</KeyCount><MaxKeys>1000</MaxKeys><IsTruncated>false</IsTruncated>' + objects + '</ListBucketResult>').encode()
        self.send_response(200)
        self.send_header('Content-Type', 'application/xml')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

results = {}
with http.server.ThreadingHTTPServer(('127.0.0.1', 0), S3) as server:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = connection('s3', {'endpoint': f'http://127.0.0.1:{server.server_port}', 'bucket': 'audit'})
    args = BASE + ['--allow-insecure-http', 'list', '--connection', config, '--max-items', '1']
    first = subprocess.run(args, env=ENV, capture_output=True, text=True, timeout=10)
    first_json = json.loads(first.stdout)
    assert first.returncode == 2 and first_json['error']['code'] == 'CLI_LIST_PAGINATION_REQUIRED', first_json
    complete = subprocess.run(args + ['--all'], env=ENV, capture_output=True, text=True, timeout=10)
    value = json.loads(complete.stdout)
    assert complete.returncode == 0, value
    assert [item['key'] for item in value['result']['objects']] == ['a', 'b', 'c'], value
    assert value['result']['next_cursor'] is None and value['result']['truncated'] is False
    bounded = subprocess.run(args + ['--all', '--max-list-items', '2'], env=ENV, capture_output=True, text=True, timeout=10)
    bound = json.loads(bounded.stdout)
    assert bounded.returncode == 4 and bound['error']['code'] == 'CLI_LIST_LIMIT_EXCEEDED', bound
    cursor = subprocess.run(args + ['--cursor', 'cursor://expired'], env=ENV, capture_output=True, text=True, timeout=10)
    assert json.loads(cursor.stdout)['error']['code'] == 'CLI_CURSOR_SESSION_REQUIRED'
    results['cli_pagination'] = {'status': 'PASS', 'pages': 3, 'bounded': True}

    destination = WORK / 'existing-output.txt'
    get_args = BASE + ['--allow-insecure-http', 'get', '--connection', config,
                       '--key', 'object', '--output', str(destination)]
    destination.write_bytes(b'original')
    conflict = subprocess.run(get_args + ['--overwrite', 'false'], env=ENV, capture_output=True, text=True, timeout=10)
    assert conflict.returncode == 5 and json.loads(conflict.stdout)['error']['code'] == 'OUTPUT_EXISTS', conflict.stdout
    assert destination.read_bytes() == b'original'
    limited = subprocess.run(get_args + ['--overwrite', 'true', '--max-transfer-bytes', '1'], env=ENV, capture_output=True, text=True, timeout=10)
    assert limited.returncode == 4, limited.stdout
    assert destination.read_bytes() == b'original'
    replaced = subprocess.run(get_args + ['--overwrite', 'true'], env=ENV, capture_output=True, text=True, timeout=10)
    assert replaced.returncode == 0 and destination.read_bytes() == b'qualified download', replaced.stdout
    assert not list(WORK.glob('.existing-output.txt.plenora-storage-*.part'))
    results['cli_download_publication'] = {'status': 'PASS', 'existing_file_preserved_on_failure': True}
    server.shutdown()
    thread.join()

# Bounded connection reads reject oversized files before JSON decoding.
large = WORK / 'oversized-connection.json'
large.write_bytes(b' ' * 1_048_577)
rejected = subprocess.run(BASE + ['test', '--connection', str(large)], env=ENV, capture_output=True, text=True, timeout=10)
assert rejected.returncode == 4 and json.loads(rejected.stdout)['error']['code'] == 'CONNECTION_FILE_TOO_LARGE'
results['connection_file_bound'] = {'status': 'PASS'}

seen = threading.Event()
release = threading.Event()
commands = []
class FTP(socketserver.StreamRequestHandler):
    def handle(self):
        self.wfile.write(b'220 audit\r\n')
        while line := self.rfile.readline():
            verb = line.decode().strip().split(' ')[0]
            commands.append(verb)
            if verb == 'MLST':
                seen.set()
                release.wait(8)
                return
            replies = {'USER': b'331 password\r\n', 'PASS': b'230 logged in\r\n', 'TYPE': b'200 binary\r\n', 'CWD': b'250 directory\r\n'}
            self.wfile.write(replies.get(verb, b'500 unsupported\r\n'))

with socketserver.ThreadingTCPServer(('127.0.0.1', 0), FTP) as server:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config = connection('ftp', {'host': '127.0.0.1', 'port': server.server_address[1]})
    source = WORK / 'input.txt'
    source.write_text('audit')
    deadline = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=2)
    args = BASE + ['--allow-insecure-ftp', '--deadline', deadline.isoformat(), 'put', '--connection', config, '--key', 'parent/object', '--input', str(source), '--overwrite', 'true', '--publication-policy', 'best-effort']
    started = time.monotonic()
    process = subprocess.Popen(args, env=ENV, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert seen.wait(3), 'FTP parent probe was not reached'
        try:
            stdout, stderr = process.communicate(timeout=4)
            value = json.loads(stdout)
            assert process.returncode == 5 and value['error']['category'] == 'timeout', value
            assert value['error']['remote_effect'] == 'none', value
            results['ftp_parent_deadline'] = {'status': 'PASS', 'elapsed': round(time.monotonic() - started, 2)}
        except subprocess.TimeoutExpired:
            raise AssertionError('FTP directory probe exceeded its deadline') from None
    finally:
        if process.poll() is None:
            process.kill()
        process.communicate()
        release.set()
        server.shutdown()
        thread.join()

evidence = {'binary_sha256': hashlib.sha256(CLI.read_bytes()).hexdigest(), 'results': results}
(WORK / 'results.json').write_text(json.dumps(evidence, indent=2), encoding='utf-8')
print(json.dumps(evidence, indent=2))
