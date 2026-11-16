"""Release cycle policy and fail-closed resource oracle (runtime quartile model)."""
from statistics import median

RELEASE_POLICY_ID = 'storage-release-cycles-v1'

# Forty samples yield two ten-sample windows separated by 30 cycles.
# A retained 1 MiB/cycle therefore produces about 30 MiB of window growth,
# exceeding 16 MiB while staying below the retained 128 MiB absolute cap.
# Local allocator calibration is required evidence, not provider qualification.
POLICY = {
    'id': 'storage-sdk-cycles-v1', 'cycles': 40, 'warmup_cycles': 5,
    'absolute_growth': {'rss_bytes': 128 * 1024**2, 'file_descriptors': 16, 'threads': 16, 'temporary_files': 0},
    'window_growth': {'rss_bytes': 16 * 1024**2, 'file_descriptors': 2, 'threads': 2, 'temporary_files': 0},
}


def analyze(samples):
    if len(samples) != POLICY['cycles']:
        raise ValueError('resource cycle inventory is incomplete')
    result = {}
    for key, absolute in POLICY['absolute_growth'].items():
        values = [row[key] for row in samples]
        if any(type(v) is not int or v < (0 if key == 'temporary_files' else 1) for v in values):
            raise ValueError('invalid resource sample')
        if key == 'temporary_files' and any(values):
            raise ValueError('owned temporary file remains after cycle')
        width = len(values) // 4
        growth = median(values[-width:]) - median(values[:width])
        result[key] = dict(first_quartile=median(values[:width]), last_quartile=median(values[-width:]),
                           window_growth=growth, growth_per_cycle=growth / (len(values) - width),
                           peak=max(values), baseline=values[0])
        if max(values) - values[0] > absolute or growth > POLICY['window_growth'][key]:
            raise ValueError('resource growth exceeds cycle policy: ' + key)
    return result


def validate_cycles(report):
    if report.get('schema_version') != 2 or report.get('policy') != POLICY:
        raise ValueError('resource policy differs')
    if report.get('completed_cycles') != POLICY['cycles'] or report.get('warmup_completed') != POLICY['warmup_cycles']:
        raise ValueError('cycle budget incomplete')
    actual = analyze(report['samples'])
    if report.get('analysis') != actual:
        raise ValueError('resource summary differs from samples')
    for label in ('after_cancel', 'after_close'):
        residual(report['before_open'], report[label])
    if len(report['lifecycle']) != POLICY['cycles']:
        raise ValueError('lifecycle cycle inventory incomplete')
    for cycle in report['lifecycle']:
        if [row['mode'] for row in cycle] != report.get('upload_modes', ['buffered']):
            raise ValueError('lifecycle strategy inventory incomplete')
        for row in cycle:
            if row.get('cancel_phase') != 'resolver':
                raise ValueError('cancellation did not prove the in-flight resolver boundary')
            for phase in ('cancel', 'close'):
                residual(row[phase + '_before'], row[phase + '_after'])


def residual(before, after):
    for key in POLICY['absolute_growth']:
        limit = POLICY['window_growth'][key] if key == 'rss_bytes' else 0
        if (type(before[key]) is not int or before[key] < 0 or type(after[key]) is not int
                or after[key] < 0 or after[key] > before[key] + limit):
            raise ValueError('owned resource residue: ' + key)


def validate_calibration(report):
    if report.get('policy') != POLICY or report.get('status') != 'PASS' or len(report['normal']) != 3:
        raise ValueError('calibration policy or repetitions differ')
    for run in report['normal']:
        if run['status'] != 'PASS' or analyze(run['samples']) != run['analysis']:
            raise ValueError('calibration noise failed')
    control = report['leak_control']
    if control['status'] != 'FAIL' or control['injected_bytes_per_cycle'] != 1024**2:
        raise ValueError('missing leak control')
    samples = control['samples']
    if len(samples) != POLICY['cycles'] or any(
            type(row.get(key)) is not int or row[key] < 0 for row in samples for key in POLICY['absolute_growth']):
        raise ValueError('invalid leak control samples')
    rss = [row['rss_bytes'] for row in samples]
    if median(rss[-10:]) - median(rss[:10]) <= POLICY['window_growth']['rss_bytes']:
        raise ValueError('leak control did not exceed the RSS trend threshold')
    try:
        analyze(control['samples'])
    except ValueError:
        # Missing samples are not evidence that the leak detector worked.
        if len(control['samples']) != POLICY['cycles']:
            raise ValueError('incomplete leak control') from None
    else:
        raise ValueError('leak control was not detected')
