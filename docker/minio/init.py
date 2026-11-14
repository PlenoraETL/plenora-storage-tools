"""Create only the two dedicated fixture buckets using public test credentials."""
from datetime import datetime, timezone
import hashlib
import hmac
import ssl
from urllib.parse import urlsplit
from urllib.request import Request
from xml.etree import ElementTree
from fixture_readiness import FixtureFailure, exchange, wait_ready


def signed_request(endpoint, method):
    now = datetime.now(timezone.utc)
    timestamp, date = now.strftime('%Y%m%dT%H%M%SZ'), now.strftime('%Y%m%d')
    body_hash = hashlib.sha256(b'').hexdigest()
    host = urlsplit(endpoint).netloc
    headers = f'host:{host}\nx-amz-content-sha256:{body_hash}\nx-amz-date:{timestamp}\n'
    signed = 'host;x-amz-content-sha256;x-amz-date'
    canonical = f'{method}\n/plenora-test\n\n{headers}\n{signed}\n{body_hash}'
    scope = f'{date}/us-east-1/s3/aws4_request'
    to_sign = f'AWS4-HMAC-SHA256\n{timestamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}'
    key = b'AWS4plenora-dev-secret'
    for part in [date, 'us-east-1', 's3', 'aws4_request']:
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    return Request(endpoint + '/plenora-test', data=b'' if method == 'PUT' else None, method=method, headers={
        'x-amz-date': timestamp, 'x-amz-content-sha256': body_hash,
        'Authorization': f'AWS4-HMAC-SHA256 Credential=plenora-dev/{scope}, SignedHeaders={signed}, Signature={signature}',
    })


def create_bucket(endpoint, context):
    status, body = exchange(signed_request(endpoint, 'PUT'), 'create_bucket',
                            context=context, allowed=(200, 409))
    if status == 409:
        try:
            owned = ElementTree.fromstring(body).findtext('Code') == 'BucketAlreadyOwnedByYou'
        except ElementTree.ParseError:
            owned = False
        if not owned:
            raise FixtureFailure('bucket_conflict', 'create_bucket', status=409)
    # Creation or an ownership conflict is not proof the bucket is usable.
    exchange(signed_request(endpoint, 'HEAD'), 'verify_bucket', context=context, missing=True)


def main():
    for service, endpoint in [('minio', 'http://minio:9000'), ('minio-tls', 'https://minio-tls:9000')]:
        try:
            context = ssl.create_default_context(cafile='/certs/ca.crt') if endpoint.startswith('https:') else None
        except OSError:
            print(FixtureFailure('configuration', 'tls_setup').diagnostic(service, 1))
            return 1
        if not wait_ready(service, lambda: create_bucket(endpoint, context)):
            return 1
    print('PASS isolated HTTP and verified HTTPS fixture buckets')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
