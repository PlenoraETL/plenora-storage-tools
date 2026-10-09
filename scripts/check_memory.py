"""Reserve available memory before work that holds payloads in fixture memory.

The fake GCS fixture keeps objects in memory: in the spooled 1 GiB transfers
it holds the source and its server-side copy at once, about 2 GiB at its
peak. Like check_disk_space.py, this refuses to start when the host cannot
take that peak plus a reserve for every other fixture and the runner, and
never frees anything by itself.
"""
import argparse
import json
from pathlib import Path

GIB = 1024**3
# Peak of the fake GCS fixture in the spooled 1 GiB transfers: source and copy.
GCS_PEAK_BYTES = 2 * GIB
# Reserve for every other fixture, the runner and the operating system.
RESERVE_BYTES = 2 * GIB


def available(meminfo=Path('/proc/meminfo')):
    """MemAvailable in bytes, as the kernel estimates it for new work."""
    for line in meminfo.read_text().splitlines():
        if line.startswith('MemAvailable:'):
            return int(line.split()[1]) * 1024
    raise ValueError('MemAvailable is not reported')


def inspect(free, peak=GCS_PEAK_BYTES, reserve=RESERVE_BYTES):
    required = peak + reserve
    return {'schema_version': 1, 'available_bytes': free, 'required_bytes': required,
            'gcs_peak_bytes': peak, 'reserve_bytes': reserve,
            'status': 'PASS' if free >= required else 'FAIL'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    args = parser.parse_args()
    report = inspect(available())
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    if report['status'] != 'PASS':
        raise SystemExit('insufficient memory for the in-memory GCS fixture peak; nothing was started')
    print('PASS qualification memory headroom')


if __name__ == '__main__':
    main()
