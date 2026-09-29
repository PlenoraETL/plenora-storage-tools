"""Publish a sealed bundle through the gated workflow, resuming the same draft safely."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from campaign_state import digest, exclusive, write_json
from release_publication import check_files, extract
from versioning import ROOT, workspace_version


def validate_input(bundle, version, log):
    """Requalify before creating any public tag or GitHub draft."""
    with tempfile.TemporaryDirectory(prefix='storage-publication-preflight-') as temporary:
        source = extract(bundle, temporary, 'qualification')
        with log.open('wb') as stream:
            result = subprocess.run([sys.executable, str(ROOT / 'scripts/qualify_release.py'),
                                     str(source / 'dist' / version), '--evidence', str(source / 'evidence')],
                                    cwd=ROOT, stdout=stream, stderr=subprocess.STDOUT)
        if result.returncode:
            raise ValueError('publication input did not qualify; no tag or draft created')


def publish(bundle, output, repository, notes, retry_workflow=False):
    bundle, output, notes = bundle.resolve(), output.resolve(), notes.resolve()

    def command(*args):
        return subprocess.check_output(args, cwd=ROOT, text=True).strip()

    revision = command('git', 'rev-parse', 'HEAD')
    version = workspace_version().native
    tag = 'v' + version
    if command('git', 'status', '--porcelain') or command('git', 'ls-remote', 'origin', 'refs/heads/main').split()[0] != revision:
        raise ValueError('publication requires unchanged clean source on remote main')
    identity = {'source_revision': revision, 'version': version, 'bundle_sha256': digest(bundle),
                'repository': repository, 'notes_sha256': digest(notes)}
    with exclusive(output):
        state_path = output / 'publication.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {'identity': identity}
        if state['identity'] != identity:
            raise ValueError('publication inputs changed')
        validate_input(bundle, version, output / 'preflight.log')

        def save(status, **values):
            state.update(status=status, **values)
            write_json(state_path, state)
            print(status, flush=True)

        def gh(*args):
            return command('gh', *args, '--repo', repository)

        def release():
            rows = json.loads(gh('release', 'list', '--limit', '100', '--json', 'tagName,isDraft,isPrerelease'))
            return next((row for row in rows if row['tagName'] == tag), None)

        refs = {row.split()[1]: row.split()[0] for row in command('git', 'ls-remote', 'origin',
                        'refs/tags/' + tag, 'refs/tags/' + tag + '^{}').splitlines()}
        if refs:
            if refs.get('refs/tags/' + tag + '^{}', refs.get('refs/tags/' + tag)) != revision:
                raise ValueError('existing release tag identifies different source')
        else:
            existing = subprocess.run(['git', 'show-ref', '--verify', '--quiet', 'refs/tags/' + tag], cwd=ROOT)
            if existing.returncode == 1:
                command('git', 'tag', '-a', tag, revision, '-m', 'Qualified Plenora Storage Tools ' + version)
            elif existing.returncode != 0 or command('git', 'rev-parse', tag + '^{}') != revision:
                raise ValueError('local tag differs or cannot be inspected')
            command('git', 'push', 'origin', 'refs/tags/' + tag)
        current = release()
        if current is None:
            gh('release', 'create', tag, '--verify-tag', '--draft', '--title', 'Plenora Storage Tools ' + version,
               '--notes-file', str(notes))
            current = release()
        if current['isDraft']:
            save('checking-draft')
            metadata = json.loads(gh('release', 'view', tag, '--json', 'assets'))
            assets = [asset for asset in metadata['assets'] if asset['name'] == 'qualification-input.tar.gz']
            if assets:
                with tempfile.TemporaryDirectory(prefix='storage-draft-check-') as temporary:
                    gh('release', 'download', tag, '--pattern', 'qualification-input.tar.gz', '--dir', temporary)
                    if digest(Path(temporary) / 'qualification-input.tar.gz') != identity['bundle_sha256']:
                        raise ValueError('draft contains a different qualification input; it will not be overwritten')
            else:
                if bundle.name != 'qualification-input.tar.gz':
                    raise ValueError('sealed input must use the workflow filename')
                gh('release', 'upload', tag, str(bundle))
            identifier = state.get('workflow_run')
            if identifier:
                prior = json.loads(gh('run', 'view', identifier, '--json', 'status,conclusion,headSha,displayTitle'))
                if prior['status'] == 'completed' and prior['conclusion'] != 'success':
                    if not retry_workflow:
                        raise ValueError('publication workflow failed; inspect before explicitly retrying')
                    state.setdefault('failed_workflows', []).append(identifier)
                    state.pop('workflow_run')
                    state.pop('dispatch_started', None)
                    identifier = None
            if not identifier:
                if 'dispatch_started' not in state:
                    save('dispatching', dispatch_started=datetime.now(timezone.utc).isoformat())
                    gh('workflow', 'run', 'release.yml', '--ref', 'main', '-f', 'tag=' + tag)
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    runs = json.loads(gh('run', 'list', '--workflow', 'release.yml', '--event', 'workflow_dispatch',
                                         '--limit', '30', '--json', 'databaseId,headSha,displayTitle,createdAt'))
                    started = datetime.fromisoformat(state['dispatch_started']).timestamp()
                    matches = [row for row in runs if row['headSha'] == revision and row['displayTitle'] == 'Publish ' + tag
                               and datetime.fromisoformat(row['createdAt'].replace('Z', '+00:00')).timestamp() >= started - 5]
                    if len(matches) > 1:
                        raise ValueError('multiple publication workflows match this dispatch')
                    if matches:
                        identifier = str(matches[0]['databaseId'])
                        save('waiting-for-publication', workflow_run=identifier)
                        break
                    time.sleep(5)
                else:
                    raise ValueError('dispatch outcome is unknown; inspect Actions before changing the checkpoint')
            deadline = time.monotonic() + 2 * 3600
            while time.monotonic() < deadline:
                report = json.loads(gh('run', 'view', identifier, '--json', 'headSha,status,conclusion,displayTitle'))
                if report['headSha'] != revision or report['displayTitle'] != 'Publish ' + tag:
                    raise ValueError('publication workflow identifies a different release')
                if report['status'] == 'completed':
                    if report['conclusion'] != 'success':
                        save('workflow-failed')
                        raise ValueError('publication failed; draft and evidence retained')
                    break
                time.sleep(30)
            else:
                raise TimeoutError('publication workflow did not finish')
        current = json.loads(gh('release', 'view', tag, '--json', 'isDraft,isPrerelease,url,tagName'))
        if current['isDraft'] or current['isPrerelease'] or current['tagName'] != tag:
            raise ValueError('stable publication is not complete')
        downloaded = output / ('download-' + str(time.time_ns()))
        gh('release', 'download', tag, '--dir', str(downloaded))
        check_files(downloaded)
        receipt = json.loads((downloaded / 'release-qualification.json').read_text())
        if receipt['source_revision'] != revision or receipt['status'] != 'qualified_for_publication':
            raise ValueError('published qualification differs')
        save('published-and-download-verified', release_url=current['url'],
             receipt_sha256=digest(downloaded / 'release-qualification.json'))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bundle', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--repository', required=True)
    parser.add_argument('--notes', required=True, type=Path)
    parser.add_argument('--retry-failed-workflow', action='store_true')
    args = parser.parse_args()
    publish(args.bundle, args.output, args.repository, args.notes, args.retry_failed_workflow)
