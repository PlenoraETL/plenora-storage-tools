"""Local artifact action executor, using the action options in the CI plan.

This proves the handoff contract on disjoint filesystems, not GitHub transport.
"""
import fnmatch
import hashlib
from pathlib import Path


def sha(data):
    return hashlib.sha256(data).hexdigest()


def confined(root, relative):
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()):
        raise ValueError('artifact path outside workspace')
    return path


class ArtifactExecutor:
    def __init__(self, workspaces):
        self.workspaces = workspaces
        self.store = {}

    def __call__(self, identifier, node, subject, incoming):
        job = node['context']['job']
        root = self.workspaces[job]
        if node['kind'] == 'job':
            return 'PASS', {'completed': job}
        step = node['definition']
        action = step.get('uses', '').split('@')[0]
        options = step.get('with', {})
        files = {}
        if action == 'actions/upload-artifact':
            name = options['name']
            if name in self.store:
                raise ValueError('artifact name already produced')
            for expression in options['path'].splitlines():
                selected = confined(root, expression)
                paths = selected.rglob('*') if selected.is_dir() else root.glob(expression)
                for path in paths:
                    if path.is_file():
                        confined(root, path.relative_to(root))
                        relative = path.relative_to(selected).as_posix() if selected.is_dir() else path.name
                        if relative in files:
                            raise ValueError('duplicate artifact member')
                        files[relative] = path.read_bytes()
            if not files:
                raise ValueError('artifact upload empty')
            self.store[name] = {'producer': job, 'files': files, 'subject': subject}
            observed = {name + '/' + key: sha(value) for key, value in files.items()}
        elif action == 'actions/download-artifact':
            matched = {name: asset for name, asset in self.store.items()
                       if name == options.get('name') or fnmatch.fnmatchcase(name, options.get('pattern', ''))}
            if not matched:
                raise ValueError('artifact download empty')
            observed = {}
            needs = node['context']['job_settings'].get('needs', [])
            needs = [needs] if isinstance(needs, str) else needs
            for name, asset in matched.items():
                if asset['producer'] not in needs or asset['subject'] != subject:
                    raise ValueError('artifact producer or subject differs')
                destination = confined(root, options['path'])
                if 'pattern' in options and not options.get('merge-multiple', False):
                    destination = confined(destination, name)
                for relative, data in asset['files'].items():
                    path = confined(destination, relative)
                    if path.exists():
                        raise ValueError('artifact download collision')
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(data)
                    observed[path.relative_to(root).as_posix()] = sha(path.read_bytes())
        else:
            raise ValueError('unsupported local artifact action')
        return 'PASS', {'action': action, 'files': observed}
