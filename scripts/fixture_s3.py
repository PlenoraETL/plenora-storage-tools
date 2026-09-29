"""Minimal signed requests for disposable MinIO fixtures, using public test keys."""
from datetime import datetime, timezone
import hashlib
import hmac
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import Request, urlopen
from xml.etree import ElementTree


def request(endpoint, method, path, body=b'', headers=None):
    now = datetime.now(timezone.utc)
    timestamp, date = now.strftime('%Y%m%dT%H%M%SZ'), now.strftime('%Y%m%d')
    body_hash = hashlib.sha256(body).hexdigest()
    fields = {'host': urlsplit(endpoint).netloc, 'x-amz-content-sha256': body_hash,
              'x-amz-date': timestamp, **(headers or {})}
    names = ';'.join(sorted(fields))
    canonical_headers = ''.join(f'{name}:{fields[name]}\n' for name in sorted(fields))
    canonical = f'{method}\n{path}\n\n{canonical_headers}\n{names}\n{body_hash}'
    scope = f'{date}/us-east-1/s3/aws4_request'
    to_sign = f'AWS4-HMAC-SHA256\n{timestamp}\n{scope}\n{hashlib.sha256(canonical.encode()).hexdigest()}'
    key = b'AWS4plenora-dev-secret'
    for part in (date, 'us-east-1', 's3', 'aws4_request'):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    fields['authorization'] = f'AWS4-HMAC-SHA256 Credential=plenora-dev/{scope}, SignedHeaders={names}, Signature={signature}'
    try:
        with urlopen(Request(endpoint + path, data=body, method=method, headers=fields), timeout=30) as response:
            return response.status, response.read()
    except HTTPError as error:
        return error.code, error.read(65536)


def error_code(body):
    """Expose only the allowlisted server category, never response messages."""
    try:
        code = ElementTree.fromstring(body).findtext('Code')
    except ElementTree.ParseError:
        return 'Unclassified'
    return code if code in {'XMinioStorageFull', 'AccessDenied', 'NoSuchKey'} else 'Unclassified'
