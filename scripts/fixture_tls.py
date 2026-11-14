"""Generate ephemeral fixture identities and verify them with strict Python TLS."""
import argparse
import ipaddress
import os
from pathlib import Path
import re
import ssl
import subprocess


def verify_pair(cert, key, trust, hostname):
    """Complete a real TLS handshake in memory, without a server or Docker."""
    client_context = ssl.create_default_context(cafile=str(trust))
    client_context.verify_flags |= ssl.VERIFY_X509_STRICT
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert, key)
    incoming, outgoing = ssl.MemoryBIO(), ssl.MemoryBIO()
    server_in, server_out = ssl.MemoryBIO(), ssl.MemoryBIO()
    client = client_context.wrap_bio(incoming, outgoing, server_hostname=hostname)
    server = server_context.wrap_bio(server_in, server_out, server_side=True)
    completed = set()
    for _ in range(20):
        for name, peer in (('client', client), ('server', server)):
            if name not in completed:
                try:
                    peer.do_handshake()
                    completed.add(name)
                except ssl.SSLWantReadError:
                    pass
        if completed == {'client', 'server'}:
            return
        server_in.write(outgoing.read())
        incoming.write(server_out.read())
    raise ValueError('fixture TLS handshake did not complete')


def generate(kind, root, host='127.0.0.1'):
    root = Path(root).resolve()
    root.mkdir(parents=True, exist_ok=True)

    def openssl(*arguments):
        subprocess.run(['openssl', *arguments], cwd=root, check=True,
                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)

    if kind == 'minio':
        (root / 'minio').mkdir(exist_ok=True)
        openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
                '-keyout', 'ca.key', '-out', 'ca.crt', '-subj', '/CN=Plenora storage test CA',
                '-addext', 'basicConstraints=critical,CA:TRUE',
                '-addext', 'keyUsage=critical,keyCertSign,cRLSign',
                '-addext', 'subjectKeyIdentifier=hash')
        openssl('req', '-new', '-newkey', 'rsa:2048', '-nodes', '-subj', '/CN=minio-tls',
                '-keyout', 'minio/private.key', '-out', 'minio.csr')
        (root / 'minio.ext').write_text(
            'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\n'
            'extendedKeyUsage=serverAuth\nsubjectAltName=DNS:minio-tls\n'
            'subjectKeyIdentifier=hash\nauthorityKeyIdentifier=keyid:always\n', encoding='ascii')
        openssl('x509', '-req', '-days', '2', '-in', 'minio.csr', '-CA', 'ca.crt',
                '-CAkey', 'ca.key', '-CAcreateserial', '-extfile', 'minio.ext',
                '-out', 'minio/public.crt')
        verify_pair(root / 'minio/public.crt', root / 'minio/private.key', root / 'ca.crt', 'minio-tls')
    elif kind == 'ftps':
        try:
            identity = 'IP:' + str(ipaddress.ip_address(host))
        except ValueError:
            if not re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?', host):
                raise ValueError('invalid fixture TLS host') from None
            identity = 'DNS:' + host
        openssl('req', '-x509', '-newkey', 'rsa:2048', '-nodes', '-days', '2',
                '-keyout', 'server.key', '-out', 'server.crt', '-subj', '/CN=ftps',
                '-addext', 'subjectAltName=DNS:ftps,' + identity,
                '-addext', 'basicConstraints=critical,CA:FALSE',
                '-addext', 'keyUsage=critical,digitalSignature,keyEncipherment',
                '-addext', 'extendedKeyUsage=serverAuth')
        # FTPS deliberately trusts its self-signed endpoint, not a signing CA.
        for name in ('ftps', host):
            verify_pair(root / 'server.crt', root / 'server.key', root / 'server.crt', name)
    else:
        raise ValueError('unknown fixture TLS identity')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('kind', choices=('minio', 'ftps'))
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    try:
        generate(args.kind, args.output, os.environ.get('PLENORA_FIXTURE_HOST', '127.0.0.1'))
    except (OSError, ValueError, subprocess.SubprocessError):
        raise SystemExit('FAIL fixture TLS phase=certificate_setup category=configuration retry=never') from None
    print('PASS fixture TLS generation and strict handshake: ' + args.kind)


if __name__ == '__main__':
    main()
