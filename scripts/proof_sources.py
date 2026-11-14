"""Read proof inventories from producers without importing or executing them."""
import ast
from pathlib import Path
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def tree(name, root=ROOT):
    return ast.parse((root / name).read_text(encoding='utf-8'))


def dictionary_keys(name, variable, root=ROOT):
    matches = [node.value for node in ast.walk(tree(name, root))
               if isinstance(node, ast.Assign)
               and any(isinstance(t, ast.Name) and t.id == variable for t in node.targets)]
    if len(matches) != 1 or not isinstance(matches[0], ast.Dict):
        raise ValueError(f'{name}: expected one literal {variable} dictionary')
    keys = [ast.literal_eval(key) for key in matches[0].keys]
    if len(keys) != len(set(keys)):
        raise ValueError(f'{name}: duplicate {variable} key')
    return tuple(keys)


def result_keys(name, root=ROOT):
    return tuple(sorted({ast.literal_eval(target.slice)
        for node in ast.walk(tree(name, root)) if isinstance(node, ast.Assign)
        for target in node.targets if isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Name) and target.value.id == 'results'
        and isinstance(target.slice, ast.Constant)}))


def fuzz_targets(root=ROOT):
    manifest = tomllib.loads((root / 'fuzz/Cargo.toml').read_text(encoding='utf-8'))
    return tuple(entry['name'] for entry in manifest['bin'])


def passed_names(name, root=ROOT, function='passed'):
    """Enumerate literal proof labels, expanding finite producer loops only."""
    names = set()

    def value(node, bindings):
        if isinstance(node, ast.Name) and node.id in bindings:
            return bindings[node.id]
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return value(node.left, bindings) + value(node.right, bindings)
        return ast.literal_eval(node)

    def visit(node, bindings):
        if isinstance(node, ast.For) and isinstance(node.target, ast.Tuple) and isinstance(node.iter, (ast.List, ast.Tuple)):
            for item in node.iter.elts:
                if not isinstance(item, (ast.Tuple, ast.List)):
                    raise ValueError(name + ': unsupported inventory loop')
                local = dict(bindings)
                for target, expression in zip(node.target.elts, item.elts):
                    if isinstance(target, ast.Name) and isinstance(expression, ast.Constant):
                        local[target.id] = expression.value
                for child in node.body:
                    visit(child, local)
            return
        if isinstance(node, ast.For) and isinstance(node.target, ast.Name):
            try:
                values = ast.literal_eval(node.iter)
            except (ValueError, TypeError):
                values = None
            if isinstance(values, (tuple, list)):
                for item in values:
                    for child in node.body:
                        visit(child, {**bindings, node.target.id: item})
                return
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == function:
            names.add(value(node.args[0], bindings))
        for child in ast.iter_child_nodes(node):
            visit(child, bindings)

    visit(tree(name, root), {})
    if not names:
        raise ValueError(name + ': no proof labels discovered')
    return names


def providers(root=ROOT):
    manifest = tomllib.loads((root / 'crates/plenora-storage-engine/Cargo.toml').read_text(encoding='utf-8'))
    return tuple(manifest['features']['full'])
