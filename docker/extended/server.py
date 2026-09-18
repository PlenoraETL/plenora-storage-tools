"""Isolated integration servers. Credentials are public test fixtures only."""
import os
import sys
from pathlib import Path

mode = sys.argv[1]
if mode == 'smb':
    os.execvp('smbd', ['smbd', '--foreground', '--no-process-group', '--debug-stdout'])
elif mode == 'ftps':
    from pyftpdlib.authorizers import DummyAuthorizer
    from pyftpdlib.handlers import TLS_FTPHandler
    from pyftpdlib.servers import FTPServer
    authorizer = DummyAuthorizer()
    authorizer.add_user('plenora', 'plenora-fixture-secret', '/data', perm='elradfmwMT')
    TLS_FTPHandler.authorizer = authorizer
    TLS_FTPHandler.certfile = '/certs/server.crt'
    TLS_FTPHandler.keyfile = '/certs/server.key'
    TLS_FTPHandler.tls_control_required = True
    TLS_FTPHandler.tls_data_required = True
    TLS_FTPHandler.passive_ports = range(30100, 30110)
    FTPServer(('0.0.0.0', 21), TLS_FTPHandler).serve_forever()
elif mode == 'webdav':
    from cheroot.wsgi import Server
    from wsgidav.wsgidav_app import WsgiDAVApp
    app = WsgiDAVApp({
        'provider_mapping': {'/': '/data'},
        'simple_dc': {'user_mapping': {'*': {'plenora': {'password': 'plenora-fixture-secret'}}}},
        'http_authenticator': {'accept_basic': True, 'accept_digest': False, 'default_to_digest': False},
        'verbose': 1,
    })
    Server(('0.0.0.0', 8080), app).start()
elif mode == 'azure-init':
    from azure.storage.blob import BlobServiceClient
    from azure.core.exceptions import ResourceExistsError
    client = BlobServiceClient('http://azure:10000/devstoreaccount1', credential={
        'account_name': 'devstoreaccount1',
        'account_key': 'Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/K1SZFPTOtr/KBHBeksoGMGw==',
    })
    try:
        client.create_container('plenora-test')
    except ResourceExistsError:
        pass
else:
    raise SystemExit('unknown fixture')
