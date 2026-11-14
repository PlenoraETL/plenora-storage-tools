"""Application health checks whose Docker diagnostics contain no server data."""
import base64
import ftplib
import ssl
import subprocess
import sys
from urllib.request import Request
from fixture_readiness import FixtureFailure, exchange, transport_failure


def probe(service):
    if service == 'ftps':
        client = ftplib.FTP_TLS(context=ssl.create_default_context(cafile='/certs/server.crt'), timeout=5)
        try:
            client.connect('ftps', 21)
            client.auth()
            client.login('plenora', 'plenora-fixture-secret')
            client.prot_p()
            client.nlst()
        finally:
            client.close()
    elif service == 'webdav':
        token = base64.b64encode(b'plenora:plenora-fixture-secret').decode()
        exchange(Request('http://127.0.0.1:8080/', method='PROPFIND', data=b'',
                         headers={'Depth': '0', 'Authorization': 'Basic ' + token}),
                 'verify_listing', allowed=(207,))
    elif service == 'smb':
        result = subprocess.run(['smbclient', '//127.0.0.1/storage', '-U', 'plenora%plenora-fixture-secret',
                                 '-m', 'SMB3', '--option=client smb encrypt=required', '-c', 'ls'],
                                capture_output=True, timeout=5)
        if result.returncode:
            raise FixtureFailure('application', 'verify_listing')
    else:
        raise ValueError('unknown fixture health probe')


def main():
    service = sys.argv[1] if len(sys.argv) == 2 and sys.argv[1] in ('ftps', 'webdav', 'smb') else 'unknown'
    failure = None
    try:
        probe(service)
    except FixtureFailure as error:
        failure = error
    except ftplib.error_perm:
        failure = FixtureFailure('authentication', 'verify_listing')
    except ftplib.error_temp:
        failure = FixtureFailure('application', 'verify_listing', retry=True)
    except OSError as error:
        failure = transport_failure(error, 'verify_listing')
    except subprocess.TimeoutExpired:
        failure = FixtureFailure('transport', 'verify_listing', retry=True)
    except Exception:
        failure = FixtureFailure('configuration', 'probe_setup')
    if failure is not None:
        print(failure.diagnostic(service, 1))
        return 1
    print(f'PASS fixture={service} phase=application_ready')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
