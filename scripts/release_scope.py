"""Explicit qualification scope agreed for the first 1.0 release."""


def qualification_scope():
    return {
        'basis': 'dedicated_fixtures',
        'provider_systems': {
            'local': 'local_filesystem', 's3': 'minio', 'sftp': 'openssh',
            'ftp': 'pure_ftpd', 'ftps': 'pyftpdlib_tls', 'azure': 'azurite',
            'gcs': 'fake_gcs_server', 'smb': 'samba', 'webdav': 'wsgidav',
        },
        'real_cloud_services': {
            'aws_s3': 'not_qualified', 'azure_blob': 'not_qualified',
            'google_cloud_storage': 'not_qualified',
        },
        'fixture_configuration': {
            'webdav': {'server': 'WsgiDAV', 'version': '4.3.5', 'http_workers': 32,
                       'request_queue_size': 64, 'keep_alive_conn_limit': 256,
                       'wsgi_serialization': 'single_process_lock',
                       'unserialized_create_if_absent': 'not_qualified'},
        },
    }


def validate_scope(scope):
    if scope != qualification_scope():
        raise ValueError('release qualification scope differs from the agreed fixture-only policy')
