"""Create only the two dedicated fixture buckets using public test credentials."""
from datetime import datetime, timezone
import hashlib
import hmac
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree


def create_bucket(endpoint):
    now = datetime.now(timezone.utc)
    timestamp, date = now.strftime('%Y%m%dT%H%M%SZ'), now.strftime('%Y%m%d')
    body_hash = hashlib.sha256(b'').hexdigest()
    host = urlsplit(endpoint).netloc
    headers = f'host:{host}\nx-amz-content-sha256:{body_hash}\nx-amz-date:{timestamp}\n'
    signed = 'host;x-amz-content-sha256;x-amz-date'
    canonical = f'PUT\n/plenora-test\n\n{headers}\n{signed}\n{body_hash}'
    scope = f'{date}/us-east-1/s3/aws4_request'
    to_sign = f'AWS4-HMAC-SHA256\n{timestamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}'
    key = b'AWS4plenora-dev-secret'
    for part in [date, 'us-east-1', 's3', 'aws4_request']:
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    request = Request(endpoint + '/plenora-test', data=b'', method='PUT', headers={
        'x-amz-date': timestamp, 'x-amz-content-sha256': body_hash,
        'Authorization': f'AWS4-HMAC-SHA256 Credential=plenora-dev/{scope}, SignedHeaders={signed}, Signature={signature}',
    })
    context = ssl.create_default_context(cafile='/certs/ca.crt') if endpoint.startswith('https:') else None
    try:
        with urlopen(request, context=context, timeout=5) as response:
            assert response.status == 200
    except HTTPError as error:
        code = ElementTree.fromstring(error.read(65536)).findtext('Code')
        if error.code != 409 or code != 'BucketAlreadyOwnedByYou':
            raise RuntimeError('fixture bucket initialization rejected') from None


for endpoint in ['http://minio:9000', 'https://minio-tls:9000']:
    for attempt in range(30):
        try:
            create_bucket(endpoint)
            break
        except (OSError, URLError, RuntimeError):
            if attempt == 29:
                raise RuntimeError('fixture bucket initialization did not become ready') from None
            time.sleep(1)
print('PASS isolated HTTP and verified HTTPS fixture buckets')
