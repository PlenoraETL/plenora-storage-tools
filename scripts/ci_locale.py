"""Piano dai workflow Storage e ricevuta locale, senza pubblicazioni implicite.

Exit 0: esecuzione completa; 1: FAIL; 2: NON ESEGUITO. Il piano non esegue gate.
"""
import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import strict_json
import os
from pathlib import Path
import platform
import re
import shlex
import shutil
import subprocess
import sys
import uuid

from ci_plan import compile_plan, digest, workflows
from ci_locale_expr import Unsupported, evaluate, render, truth

ROOT = Path(__file__).resolve().parents[1]
NOT_RUN = 'NON ESEGUITO'
OVERRIDES = {'CARGO_BUILD_JOBS': '4', 'CARGO_INCREMENTAL': '0', 'GIT_OPTIONAL_LOCKS': '0',
             'RUSTUP_AUTO_INSTALL': '0'}
# Exact action revisions are part of the local adapter contract.
ACTION_MAP = {
    'actions/checkout@11bd71901bbe5b1630ceea73d27597364c9af683': 'checkout',
    'actions/setup-python@e797f83bcb11b83ae66e0230d6156d7c80228e7c': 'python',
    'dtolnay/rust-toolchain@6bed0761d98439e5a578e2877258200ad565ba87': 'rust',
    'actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02': 'hosted-artifact',
    'actions/download-artifact@3e5f45b2cfb9172054b4087a40e8e0b5a5461e7c': 'hosted-artifact',
    'actions/attest@1e69f48acb82d1966a394da916b4c1698aa569d6': 'hosted-attestation',
    'anchore/sbom-action/download-syft@3ad7283483fc7af8ff2b4ea19663c2d5ca935e26': 'hosted-install',
}
CHECKOUT_MAP = {'PlenoraETL/plenora-contracts': {
    'source': 'contracts/upstream/source.json', 'path': '.plenora-contracts'}}
SHELLS = {
    'bash': (['bash', '--noprofile', '--norc', '-e', '-o', 'pipefail'], '.sh'),
    'sh': (['sh', '-e'], '.sh'),
    'pwsh': (['pwsh', '-NoLogo', '-NoProfile', '-NonInteractive', '-File'], '.ps1'),
    'python': ([sys.executable], '.py'),
}


def git(root, *args):
    return subprocess.check_output(['git', '-C', str(root), *args], text=True,
                                   stderr=subprocess.DEVNULL,
                                   env={**os.environ, **OVERRIDES}).strip()


def inside(root, value):
    path = (root / value).resolve()
    if not path.is_relative_to(root.resolve()) or '.git' in path.parts:
        raise Unsupported('directory fuori workspace o riservata')
    return path


def derive(root=ROOT, profile='pr'):
    documents = workflows(root)
    for document in documents.values():
        for job in document['jobs'].values():
            if 'uses' not in job:
                job['steps'] = [step if isinstance(step, dict) and ('run' in step or 'uses' in step)
                                else {'uses': 'missing-step'} for step in job.get('steps', [])]
                if not job['steps']:
                    job['steps'] = [{'uses': 'missing-step'}]
    graph = compile_plan(documents)
    roots = {name for name, doc in documents.items() if 'pull_request' in (doc.get('on') or {})}
    if profile == 'qualifica':
        roots = {'release-candidate.yml', 'reliability.yml'} & documents.keys()
    elif profile == 'release':
        roots = {'release.yml'} & documents.keys()
    jobs = []
    for key in graph['order']:
        node = graph['nodes'][key]
        if node['kind'] != 'job':
            continue
        ctx = node['context']
        steps = [dict(id=sid, **step) for sid, step in graph['nodes'].items()
                 if step['kind'] == 'step' and sid.rsplit('/step/', 1)[0] == key]
        for step in steps:
            definition = step['definition']
            step['availability'] = 'DA VERIFICARE'
            step['availability_reason'] = 'piano soltanto; nessun comando eseguito'
            if key.split('/')[0] not in roots:
                step.update(availability=NOT_RUN, availability_reason='fuori profilo')
            elif 'uses' in definition and ACTION_MAP.get(definition['uses']) not in {'checkout', 'python', 'rust'}:
                step.update(availability=NOT_RUN, availability_reason='action non riprodotta localmente')
            elif re.search(r'\bdocker\b|fixtures|run_vm_campaign|scripts/verify.sh', definition.get('run', '')):
                step.update(availability=NOT_RUN, availability_reason='fixture Docker o VM non riprodotta sull’host')
        jobs.append(dict(key=key, id=ctx['job'], matrix=ctx['matrix'], needs=node['needs'],
                         selected=key.split('/')[0] in roots, context=ctx, steps=steps))
    represented = {(j['context']['workflow'], j['id']) for j in jobs}
    expected = {(w, j) for w, d in documents.items() for j in d['jobs']}
    if represented != expected:
        raise Unsupported('inventario job non rappresentato dal compilatore')
    return dict(schema=2, profile=profile, jobs=jobs, graph=graph,
                sources={p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                         for folder in ('.github/workflows', 'scripts', 'contracts')
                         for p in (root / folder).rglob('*') if p.is_file()
                         and '__pycache__' not in p.parts and p.suffix != '.pyc'},
                action_map=ACTION_MAP, checkout_map=CHECKOUT_MAP,
                filters='sovrainsieme PR; filtri evento conservati nei workflow_settings')


def aggregate(statuses):
    statuses = list(statuses)
    return 'FAIL' if 'FAIL' in statuses else 'PASS' if statuses and all(
        s == 'PASS' for s in statuses) else NOT_RUN


def command_reason(command, root, seen=None):
    """Inspect script prerequisites too, without importing or executing them."""
    seen = set() if seen is None else seen
    if re.search(r'\b(?:docker|podman|virsh|VBoxManage)\b|prepare.*fixtures|run_vm_campaign|scripts/verify.sh', command):
        return 'fixture Docker o VM non riproducibile su questo host locale'
    if re.search(r'\b(?:sudo|apt-get|choco|brew)\b|\bpip\s+install\b|\bcargo\s+install\b', command):
        return 'provisioning richiesto; preparare separatamente ambiente e dipendenze'
    if re.search(r'\bgh\s|git\s+(?:push|fetch|clone|pull|checkout|reset)|cargo\s+publish|twine\s+upload', command):
        return 'operazione remota o mutazione Git non ammessa dal runner locale'
    if re.search(r'scripts/(?:publish_qualified|release_publication)\.py', command):
        return 'superficie di pubblicazione non riprodotta dal runner locale'
    for name in re.findall(r'\b(scripts/[\w-]+\.(?:py|sh|ps1))\b', command):
        if name in seen:
            continue
        seen.add(name)
        path = inside(root, name)
        if not path.is_file():
            return 'script richiesto assente'
        reason = command_reason(path.read_text(encoding='utf-8'), root, seen)
        if reason:
            return reason
    return ''


def action_commands(step, root, sha):
    mapping = ACTION_MAP.get(step['uses'])
    values = step.get('with', {})
    if mapping == 'checkout':
        if set(values) - {'repository', 'path', 'ref', 'fetch-depth'}:
            raise Unsupported('input checkout non riproducibile')
        location, wanted = root, sha
        if values.get('repository') or values.get('path'):
            contract = CHECKOUT_MAP.get(values.get('repository'))
            if not contract or values.get('path') != contract['path']:
                raise Unsupported('checkout aggiuntivo non mappato')
            source = strict_json.loads((root / contract['source']).read_text(encoding='utf-8'))
            wanted = source['revision']
            if values.get('ref') != wanted or not re.fullmatch('[0-9a-f]{40}', wanted):
                raise Unsupported('checkout contratti non al pin dichiarato')
            location = inside(root, contract['path'])
            if not location.is_dir():
                raise Unsupported('checkout contratti al pin assente; nessun clone automatico')
        elif values.get('ref', sha) != sha:
            raise Unsupported('ref checkout diversa dal soggetto locale')
        checks = [(['git', '-C', str(location), 'rev-parse', 'HEAD'], wanted)]
        if values.get('fetch-depth') == '0' or values.get('fetch-depth') == 0:
            checks.append((['git', '-C', str(location), 'rev-parse', '--is-shallow-repository'], 'false'))
        return checks
    if mapping == 'python':
        if values != {'python-version-file': '.python-version'}:
            raise Unsupported('input Python non mappato')
        wanted = (root / '.python-version').read_text(encoding='utf-8').strip()
        return [([sys.executable, '-c', 'import sys; print(".".join(map(str, sys.version_info[:2])))'], wanted)]
    if mapping == 'rust':
        if set(values) - {'toolchain', 'components'} or not values.get('toolchain'):
            raise Unsupported('input Rust non mappato')
        version = str(values['toolchain'])
        checks = [(['rustc', '--version'], 'prefix:rustc ' + version + ' ')]
        for component in values.get('components', '').split(','):
            if component.strip():
                checks.append((['rustup', 'component', 'list', '--installed', '--toolchain', version],
                               'component:' + component.strip()))
        return checks
    raise Unsupported('action nota riservata ad Actions' if mapping else 'action o pin non mappato')


def invoke(argv, cwd, env, timeout):
    result = subprocess.run(argv, cwd=cwd, env=env, timeout=timeout,
                            capture_output=True, text=True, encoding='utf-8', errors='replace')
    return result.returncode, result.stdout, result.stderr


def run_step(step, settings, workflow, context, root, directory, sha, timeout, offline, runner=invoke):
    env = dict(OVERRIDES)
    observation = dict(command=step.get('run', step.get('uses')), argv=[], environment=env,
                       services=settings.get('services', {}), toolchain={}, executed=False)
    try:
        env.update({k: str(v) for k, v in render({**workflow.get('env', {}),
                   **settings.get('env', {}), **step.get('env', {})}, context).items()})
        env.update(OVERRIDES)
        if offline:
            env.update(CARGO_NET_OFFLINE='true', PIP_NO_INDEX='1')
        step = render(step, context)
        observation['rendered_command'] = step.get('run', step.get('uses'))
        defaults = {**workflow.get('defaults', {}).get('run', {}), **settings.get('defaults', {}).get('run', {})}
        cwd = inside(root, step.get('working-directory', defaults.get('working-directory', '.')))
        observation['cwd'] = str(cwd)
        if not cwd.is_dir():
            raise Unsupported('directory di lavoro assente')
        if settings.get('services') or settings.get('container'):
            return NOT_RUN, dict(observation, reason='servizi o container del job non riprodotti sull’host')
        if 'uses' in step:
            checks = action_commands(step, root, sha)
            observation['toolchain'] = step.get('with', {})
        else:
            command = step['run']
            reason = command_reason(command, root)
            if reason:
                return NOT_RUN, dict(observation, reason=reason)
            if offline and re.search(r'\b(?:curl|wget)\b|\bcargo\s+(?:fetch|audit)\b', command):
                raise Unsupported('passo con rete non riproducibile in offline')
            shell = step.get('shell', defaults.get('shell', 'pwsh' if os.name == 'nt' else 'bash'))
            if shell not in SHELLS:
                raise Unsupported('shell non mappata')
            # Single Python commands use the selected interpreter, avoiding PATH aliases.
            if re.fullmatch(r'python3? [^\n|;&<>]+', command) and not re.search(r'[$`]', command):
                argv = [sys.executable, *shlex.split(command)[1:]]
            else:
                prefix, suffix = SHELLS[shell]
                if not shutil.which(prefix[0]):
                    raise Unsupported('shell richiesta assente')
                path = directory / (uuid.uuid4().hex + suffix)
                if shell == 'pwsh':
                    command = "$ErrorActionPreference = 'Stop'\n" + command + '\nif ($LASTEXITCODE) { exit $LASTEXITCODE }\n'
                path.write_text(command + '\n', encoding='utf-8', newline='\n')
                argv = [*prefix, str(path)]
            checks = [(argv, None)]
        for argv, expected in checks:
            observation['argv'].append(argv)
            code, out, err = runner(argv, cwd, {**os.environ, **env}, timeout)
            observation.update(executed=True, returncode=code)
            if code:
                return 'FAIL', dict(observation, reason='comando terminato con errore')
            if expected is not None:
                valid = (out.startswith(expected[7:]) if expected.startswith('prefix:') else
                         any(line.startswith(expected[10:] + '-') for line in out.splitlines())
                         if expected.startswith('component:') else out.strip() == expected)
                if not valid:
                    return NOT_RUN, dict(observation, reason='prerequisito o SHA verificato diverso dal richiesto')
            elif re.search(r'\b(?:skipped|skipping|ignored|NON ESEGUITO)\b', out + err, re.I):
                # Zero ignored tests is normal Cargo output, not a skipped proof.
                cleaned = re.sub(r'\b0 (?:ignored|skipped)\b', '', out + err)
                if re.search(r'\b(?:skipped|skipping|ignored|NON ESEGUITO)\b', cleaned, re.I):
                    return NOT_RUN, dict(observation, reason='output dichiara prove non eseguite')
        return 'PASS', dict(observation, reason='comando eseguito con successo')
    except (Unsupported, FileNotFoundError):
        return NOT_RUN, dict(observation, reason='prerequisito, espressione o adattamento non disponibile')
    except (OSError, subprocess.SubprocessError):
        return 'FAIL', dict(observation, reason='errore o timeout durante esecuzione')
    except Exception:
        return 'FAIL', dict(observation, reason='errore dell’adattatore; dettagli privati omessi')


def execute(plan, root=ROOT, selected=(), timeout=1800, offline=False, runner=invoke):
    if plan['profile'] != 'pr':
        raise Unsupported('qualifica e release disponibili soltanto come piano')
    sha = git(root, 'rev-parse', 'HEAD')
    directory = root / 'target/ci-locale' / (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ-') + uuid.uuid4().hex[:8])
    directory.mkdir(parents=True)
    receipt = dict(schema=2, profile=plan['profile'], sha=sha, dirty=bool(git(root, 'status', '--porcelain')),
                   started_at=datetime.now(timezone.utc).isoformat(), host=platform.platform(), python=sys.version,
                   sources=plan['sources'], plan_sha256=digest(plan), overrides=OVERRIDES,
                   services_opt_in=False, network_mode='offline' if offline else 'online',
                   status=NOT_RUN, jobs=[], steps=[], toolchain={'python': sys.version, 'rust': 'NON ESEGUITO'})
    try:
        code, out, _ = invoke(['rustc', '--version'], root, {**os.environ, **OVERRIDES}, 15)
        if code == 0 and re.fullmatch(r'rustc [0-9][\w. +-]* \([\w -]+\)\s*', out):
            receipt['toolchain']['rust'] = out.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    (directory / 'piano.json').write_text(json.dumps(plan, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    rows = {}
    jobs = {j['key']: j for j in plan['jobs']}
    path = directory / 'ricevuta.json'
    def save():
        receipt['counts'] = dict(Counter(s['status'] for s in receipt['steps']))
        path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
    save()
    for key in plan['graph']['order']:
        node = plan['graph']['nodes'][key]
        jobkey = key.rsplit('/step/', 1)[0] if node['kind'] == 'step' else key
        job = jobs[jobkey]
        ctx = node['context']
        settings, workflow = ctx['job_settings'], ctx['workflow_settings']
        dependencies = [rows.get(n, {}).get('status', NOT_RUN) for n in node['needs']]
        preceding = dependencies + [r['status'] for k, r in rows.items() if k.startswith(jobkey + '/step/')]
        context = {**{'matrix.' + k: v for k, v in ctx['matrix'].items()},
                   'github.event_name': 'pull_request', 'github.sha': sha, 'github.workspace': str(root),
                   'runner.os': 'Windows' if os.name == 'nt' else 'Linux',
                   'always()': True, 'success()': all(s == 'PASS' for s in preceding),
                   'failure()': 'FAIL' in preceding, 'cancelled()': False}
        row = dict(id=key, job=jobkey, matrix=ctx['matrix'], needs=node['needs'], status=NOT_RUN,
                   reason='passo non eseguito', command=node['definition'].get('run', node['definition'].get('uses')),
                   environment=OVERRIDES, services=settings.get('services', {}), toolchain={}, executed=False)
        row['definition'] = node['definition']
        row['settings'] = ctx
        if node['kind'] == 'job':
            children = [r['status'] for k, r in rows.items() if k.startswith(key + '/step/') or k.startswith(key + '/call/')]
            row.update(status=aggregate(children), reason='aggregazione delle esecuzioni; non un test aggiuntivo')
            receipt['jobs'].append(row)
        else:
            try:
                if not job['selected'] or selected and job['key'] not in selected and job['id'] not in selected:
                    raise Unsupported('job fuori profilo o selezione')
                callerkeys = [key[:m.start()] for m in re.finditer('/call/', key)]
                for callerkey in callerkeys:
                    caller = jobs[callerkey]['context']['job_settings']
                    if caller.get('with') or caller.get('secrets') or not truth(evaluate(caller.get('if', 'true'), context)):
                        raise Unsupported('condizione o input del workflow chiamante non riprodotto')
                if not truth(evaluate(settings.get('if', 'true'), context)):
                    raise Unsupported('condizione del job falsa')
                label = render(settings.get('runs-on', ''), context)
                host = 'windows' if os.name == 'nt' else 'ubuntu'
                if not isinstance(label, str) or not label.startswith(host):
                    raise Unsupported('runner richiesto diverso dalla piattaforma host')
                step = node['definition']
                if not truth(evaluate(step.get('if', 'success()'), context)):
                    raise Unsupported('condizione falsa o dipendenze non PASS')
                status, observation = run_step(step, settings, workflow, context, root, directory, sha, timeout, offline, runner)
                row.update(observation, status=status)
            except Unsupported as exc:
                # These reasons are generated by the adapter, never by callbacks.
                row['reason'] = str(exc) if str(exc) in {
                    'job fuori profilo o selezione', 'condizione o input del workflow chiamante non riprodotto',
                    'condizione del job falsa', 'runner richiesto diverso dalla piattaforma host',
                    'condizione falsa o dipendenze non PASS'} else 'espressione non riproducibile'
            receipt['steps'].append(row)
        rows[key] = row
        save()
    active = [r['status'] for r in receipt['jobs'] if jobs[r['id']]['selected']]
    receipt['status'] = aggregate(active)
    if git(root, 'rev-parse', 'HEAD') != sha or derive(root, plan['profile'])['sources'] != plan['sources']:
        receipt['status'] = 'FAIL' if receipt['status'] == 'FAIL' else NOT_RUN
        receipt['reason'] = 'sorgenti o SHA cambiati durante la corsa'
    receipt['finished_at'] = datetime.now(timezone.utc).isoformat()
    save()
    return path, receipt


def main(argv=None):
    if hasattr(sys.stdout, 'reconfigure'):
        sys.stdout.reconfigure(encoding='utf-8')
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--piano', action='store_true')
    modes.add_argument('--esegui', action='store_true')
    parser.add_argument('--profilo', choices=['pr', 'qualifica', 'release'], default='pr')
    parser.add_argument('--job', action='append', default=[])
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--timeout', type=int, default=1800)
    args = parser.parse_args(argv)
    try:
        plan = derive(ROOT, args.profilo)
        if set(args.job) - {v for j in plan['jobs'] for v in (j['key'], j['id'])}:
            raise Unsupported('job sconosciuto')
        if args.piano:
            print(json.dumps(plan, ensure_ascii=False, indent=2))
            return 0
        path, receipt = execute(plan, ROOT, args.job, args.timeout, args.offline)
        print(f"Ricevuta: {path}\nEsito: {receipt['status']}")
        return {'PASS': 0, 'FAIL': 1, NOT_RUN: 2}[receipt['status']]
    except (ValueError, KeyError, TypeError, OSError, subprocess.SubprocessError):
        if args.esegui:
            directory = ROOT / 'target/ci-locale' / ('incompleta-' + uuid.uuid4().hex[:8])
            directory.mkdir(parents=True)
            receipt = dict(schema=2, profile=args.profilo, status=NOT_RUN,
                           reason='piano o prerequisiti non riproducibili',
                           sha=git(ROOT, 'rev-parse', 'HEAD'), command=[sys.executable, *sys.argv],
                           environment=OVERRIDES, services={}, toolchain={'python': sys.version},
                           host=platform.platform(), jobs=[], steps=[])
            path = directory / 'ricevuta.json'
            path.write_text(json.dumps(receipt, ensure_ascii=False, indent=2) + '\n', encoding='utf-8', newline='\n')
            print(f'Ricevuta incompleta: {path}')
        print('NON ESEGUITO: piano o prerequisiti non riproducibili', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
