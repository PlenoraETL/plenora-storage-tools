"""Snapshot the public surface of the installed SDK, not the checkout's Python sources."""
import argparse
import ast
import dataclasses
import inspect
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def annotation(value):
    if value is inspect.Signature.empty:
        return None
    if isinstance(value, str):
        return value
    if value is None or value is type(None):
        return 'None'
    if isinstance(value, type):
        return value.__name__
    return str(value).removeprefix('typing.')


def signature(value):
    sig = inspect.signature(value)
    return {'parameters': [{'name': item.name, 'kind': item.kind.name,
                            'annotation': annotation(item.annotation),
                            'required': item.default is inspect.Parameter.empty,
                            'default': None if item.default is inspect.Parameter.empty else item.default}
                           for item in sig.parameters.values()],
            'returns': annotation(sig.return_annotation)}


def snapshot(module):
    exports = {}
    for name in sorted(module.__all__):
        value = getattr(module, name)
        if name == '__version__':
            exports[name] = {'kind': 'str'}  # Release identity is tested separately against wheel metadata.
        elif inspect.isclass(value):
            item = {'kind': 'class', 'bases': [base.__name__ for base in value.__bases__]}
            if name == 'CancellationToken':
                # PyO3 descriptors lack Python annotations. Keep their installed
                # typed stub plus the actual runtime names, kinds and signatures.
                stub = ast.parse((Path(module.__file__).parent / '_native.pyi').read_text())
                declaration = next(node for node in stub.body if isinstance(node, ast.ClassDef) and node.name == name)
                item['stub'] = ast.dump(declaration, include_attributes=False)
                item['constructor'] = signature(value)
                item['members'] = {member: {'kind': 'property'} if inspect.isgetsetdescriptor(obj)
                                   else {'kind': 'method', **signature(obj)}
                                   for member, obj in vars(value).items() if not member.startswith('_')}
            else:
                members = {}
                for member, obj in vars(value).items():
                    if member.startswith('_') and member not in {'__init__', '__enter__', '__exit__', '__aenter__', '__aexit__'}:
                        continue
                    if isinstance(obj, property):
                        members[member] = {'kind': 'property', 'writable': obj.fset is not None,
                                           **signature(obj.fget)}
                    elif inspect.isfunction(obj):
                        members[member] = {'kind': 'async' if inspect.iscoroutinefunction(obj) else 'method',
                                           **signature(obj)}
                item['members'] = members
                if dataclasses.is_dataclass(value):
                    item['frozen'] = value.__dataclass_params__.frozen
                    item['fields'] = [{'name': field.name, 'type': annotation(field.type),
                                       'required': field.default is dataclasses.MISSING,
                                       'default': None if field.default is dataclasses.MISSING else field.default}
                                      for field in dataclasses.fields(value)]
            exports[name] = item
        elif inspect.isfunction(value):
            exports[name] = {'kind': 'function', **signature(value)}
        else:
            raise ValueError('unhandled public export: ' + name)
    error = module.StorageError({'code': 'FIXTURE', 'category': 'io', 'phase': 'read',
                                 'remote_effect': 'none', 'retry': {'kind': 'never'}, 'message': 'fixture'})
    source = ast.parse(Path(module.__file__).read_text(encoding='utf-8'))
    aliases = {target.id: ast.dump(node.value, include_attributes=False)
               for node in source.body if isinstance(node, ast.Assign) and isinstance(node.value, ast.Subscript)
               for target in node.targets if isinstance(target, ast.Name) and not target.id.startswith('_')}
    return {'schema_version': 1, 'exports': exports, 'type_aliases': aliases,
            'error_attributes': sorted(vars(error)), 'root_error': isinstance(error, module.PlenoraError)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--candidate', type=Path, help='Write a candidate without changing the baseline')
    args = parser.parse_args()
    import plenora_storage
    actual = snapshot(plenora_storage)
    if args.candidate:
        args.candidate.parent.mkdir(parents=True, exist_ok=True)
        args.candidate.write_text(json.dumps(actual, indent=2, sort_keys=True) + '\n', encoding='utf-8')
        print('CANDIDATE installed Python API')
    else:
        expected = json.loads((ROOT / 'api/python.json').read_text())
        if actual != expected:
            raise SystemExit('Installed Python API differs from api/python.json; generate a candidate and review the change')
        print('PASS installed Python API')


if __name__ == '__main__':
    main()
