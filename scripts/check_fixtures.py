"""Verify that every qualification fixture answers, from the VM that runs it.

Run after the fixture preparation. Compose must report every fixture service
running (and healthy where it declares a health check), and each published
endpoint must answer its protocol: an HTTP status line, an FTP 220 banner or an
SSH identification string. The report names services and checks only, never
addresses, credentials or server messages.
"""
import argparse
import json
from pathlib import Path
import socket
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ['docker', 'compose', '-f', 'docker-compose.yml', '-f', 'compose.extended.yml']
SERVICES = ('minio', 'minio-tls', 'sftp', 'ftp', 'azure', 'gcs', 'ftps', 'webdav', 'smb')
# (service, port, check): published ports of docker-compose.yml and compose.extended.yml.
PROBES = (('minio', 9000, 'http'), ('sftp', 2222, 'ssh'), ('ftp', 2121, 'ftp'), ('ftps', 2122, 'ftp'),
          ('azure', 10000, 'http'), ('gcs', 4443, 'http'), ('webdav', 8088, 'http'), ('smb', 1445, 'tcp'))


def probe(port, kind, timeout=10):
    with socket.create_connection(('127.0.0.1', port), timeout=timeout) as connection:
        connection.settimeout(timeout)
        if kind == 'tcp':
            return True
        if kind == 'http':
            connection.sendall(b'GET / HTTP/1.1\r\nHost: fixture\r\nConnection: close\r\n\r\n')
            return connection.recv(16).startswith(b'HTTP/1.')
        greeting = connection.recv(64)
        return greeting.startswith(b'220' if kind == 'ftp' else b'SSH-')


def containers():
    output = subprocess.check_output([*COMPOSE, 'ps', '--all', '--format', 'json'], cwd=ROOT, text=True)
    rows = []
    for line in output.splitlines():
        line = line.strip()
        if line:
            value = json.loads(line)
            rows.extend(value if isinstance(value, list) else [value])
    return {row['Service']: row for row in rows}


def inspect():
    states = containers()
    results = []
    for service in SERVICES:
        row = states.get(service, {})
        running = row.get('State') == 'running' and row.get('Health', '') in ('', 'healthy')
        results.append({'service': service, 'check': 'container', 'status': 'PASS' if running else 'FAIL'})
    for service, port, kind in PROBES:
        try:
            answered = probe(port, kind)
        except OSError:
            answered = False
        results.append({'service': service, 'check': kind, 'status': 'PASS' if answered else 'FAIL'})
    return {'schema_version': 1, 'status': 'PASS' if all(r['status'] == 'PASS' for r in results) else 'FAIL',
            'results': results}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = inspect()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    if report['status'] != 'PASS':
        failed = sorted({row['service'] for row in report['results'] if row['status'] != 'PASS'})
        sys.exit('fixtures not answering: ' + ', '.join(failed))
    print('PASS every fixture answers')


if __name__ == '__main__':
    main()
