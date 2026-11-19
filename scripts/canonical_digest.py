"""LF canonical identity for text inputs; artifacts and protocol bytes stay exact."""
import hashlib


def canonical_bytes(data):
    """Normalize CRLF and lone CR without decoding or changing other bytes."""
    return data.replace(b'\r\n', b'\n').replace(b'\r', b'\n')


def canonical_digest(path):
    return hashlib.sha256(canonical_bytes(path.read_bytes())).hexdigest()
