"""Check the adoption policy and byte-pinned upstream inventory offline.

Diagnostics are fixed text: never interpolate document values or exceptions.
The optional upstream checkout is read only; CI needs only vendored files.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

import jsonschema

ROOT = Path(__file__).resolve().parents[1]
UPSTREAM = ROOT / 'contracts/upstream'
sys.path.insert(0, str(UPSTREAM))
from conformance_checks import adoption_errors


def require(condition, message):
    if not condition:
        raise ValueError(message)


def check(upstream=None):
    source = json.loads((UPSTREAM / 'source.json').read_text())
    lock = json.loads((UPSTREAM / 'vendor-lock.json').read_text())
    require(source['revision'] == lock['revision'], 'ADOPTION: pin mismatch')
    present = {path.relative_to(UPSTREAM).as_posix() for path in UPSTREAM.rglob('*')
               if path.is_file() and '__pycache__' not in path.parts
               and 'proposed' not in path.relative_to(UPSTREAM).parts}
    require(present == set(lock['files']) | {'source.json', 'vendor-lock.json',
                                            'ratified/source.json'},
            'ADOPTION: vendor inventory mismatch')
    for name, entry in lock['files'].items():
        path = (UPSTREAM / name).resolve()
        require(path.is_relative_to(UPSTREAM.resolve()), 'ADOPTION: invalid vendor path')
        require(hashlib.sha256(path.read_bytes()).hexdigest() == entry['sha256'],
                'ADOPTION: vendor digest mismatch')
        if upstream:
            original = (upstream / entry['source']).resolve()
            require(original.is_relative_to(upstream.resolve()), 'ADOPTION: invalid source path')
            require(original.read_bytes() == path.read_bytes(), 'ADOPTION: upstream bytes differ')
    policy = json.loads((ROOT / 'contracts/adoption-policy.json').read_text())
    # Structural/semantic probe, not an artifact qualification or release receipt.
    manifest = dict(schema_version=4, component='plenora-storage-tools',
                    contracts_source=source, profile='plenora-storage-tools-profile-v1',
                    artifacts=[dict(name='policy-probe', surface='runtime', version='0',
                                    digest='sha256:' + '0' * 64,
                                    verification=['Policy probe only'])], **policy)
    schema = json.loads((UPSTREAM / 'adoption-manifest-v4.schema.json').read_text())
    require(not list(jsonschema.Draft202012Validator(schema).iter_errors(manifest)),
            'ADOPTION: manifest schema rejected')
    require(not adoption_errors(manifest), 'ADOPTION: manifest semantics rejected')
    ids = {item['id'] for item in policy['contracts'] if item['status'] == 'conforming'}
    require({'plenora-public-catalog-v2', 'plenora-runtime-binding-v2'} <= ids,
            'ADOPTION: missing version 2 contract')
    require(any(item['rule'] == 'RT-024' for item in policy['deviations']),
            'ADOPTION: missing materialization deviation')
    catalog = json.loads((UPSTREAM / 'storage-tools-v1.json').read_text())
    require(catalog['profile'] == manifest['profile'], 'ADOPTION: profile mismatch')
    require(len(catalog['operations']) == 7, 'ADOPTION: catalog inventory mismatch')
    print('PASS adoption policy, catalog and vendor hashes')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--upstream', type=Path)
    args = parser.parse_args()
    try:
        check(args.upstream)
    except (OSError, ValueError, KeyError, TypeError):
        raise SystemExit('FAIL adoption gate: invalid input or inconsistent evidence') from None


if __name__ == '__main__':
    main()
