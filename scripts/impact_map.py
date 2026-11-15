#!/usr/bin/env python3
"""Extract literal candidates from Python source without importing it."""
import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import sys

EXCLUDED = {'data', 'logs', 'plugins', 'tmp', '.venv', '.git'}
SUFFIX_NAMESPACES = {'state', 'config'}


def git(*args):
    try:
        return subprocess.run(['git', *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def relative(path):
    return Path(os.path.relpath(path, Path.cwd())).as_posix()


def front_matter(path):
    lines = Path(path).read_text(encoding='utf-8').splitlines()
    if not lines or lines[0] != '---':
        raise ValueError('missing front matter')
    end = lines.index('---', 1)
    return json.loads('\n'.join(lines[1:end]))


def literals(resources):
    result = {}
    for resource in sorted(set(resources)):
        namespace, name = resource.split(':', 1)
        if not namespace or not name:
            raise ValueError('invalid resource')
        literal = name.rsplit('.', 1)[-1] if namespace in SUFFIX_NAMESPACES else name
        result.setdefault(literal, []).append(resource)
    return result


def candidates(root, diagnostics):
    root = Path(root).absolute()
    if not root.is_dir():
        diagnostics.append({'path': relative(root), 'reason': 'missing_root'})
        return
    resolved = root.resolve()
    tracked = git('ls-files', '-z', '--', str(root))
    tracked_set = None if tracked is None else {Path(os.fsdecode(p)).absolute() for p in tracked.split(b'\0') if p}
    for directory, dirs, files in os.walk(root, followlinks=False):
        base = Path(directory)
        if base != root and (base / '.git').exists():
            dirs[:] = []
            continue
        for name in sorted(dirs + files):
            path = base / name
            if name in EXCLUDED:
                continue
            if path.is_symlink() and not path.resolve().is_relative_to(resolved):
                diagnostics.append({'path': relative(path), 'reason': 'symlink_outside_root'})
        dirs[:] = sorted(d for d in dirs if d not in EXCLUDED and not (base / d).is_symlink())
        for name in sorted(files):
            path = base / name
            if not name.endswith('.py') or name.startswith('.') or path.is_symlink():
                continue
            if tracked_set is not None and path not in tracked_set:
                continue
            yield path, root


class Visitor(ast.NodeVisitor):
    def __init__(self, lookup, path, package, records):
        self.lookup, self.path, self.package, self.records = lookup, path, package, records
        self.stack = []

    def scope(self, node):
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = scope
    visit_AsyncFunctionDef = scope
    visit_ClassDef = scope

    def add(self, literal, kind, line):
        for resource in self.lookup.get(literal, []):
            symbol = '.'.join(self.stack) or '<module>'
            key = (resource, self.path, symbol, kind)
            if key not in self.records or line < self.records[key]['reference_line']:
                self.records[key] = dict(resource=resource, path=self.path, symbol=symbol, package=self.package, match_kind=kind, reference_line=line)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            self.add(node.value, 'string', node.lineno)

    def visit_Attribute(self, node):
        self.add(node.attr, 'attribute', node.lineno)
        self.generic_visit(node)

    def visit_keyword(self, node):
        if node.arg is not None:
            self.add(node.arg, 'keyword', node.lineno)
        self.generic_visit(node)


def extract(args):
    lookup = literals(front_matter(args.page)['resources'])
    diagnostics, records = [], {}
    inputs = size = 0
    roots = sorted(set(relative(Path(r).absolute()) for r in args.root))
    for root in roots:
        for path, base in candidates(root, diagnostics):
            rel = relative(path)
            try:
                data = path.read_bytes()
                inputs += 1
                size += len(data)
                tree = ast.parse(data, filename=rel)
            except (SyntaxError, UnicodeDecodeError, ValueError, OSError):
                diagnostics.append({'path': rel, 'reason': 'parse_error'})
                continue
            parts = path.relative_to(base).parts
            package = (Path(root) / parts[0]).as_posix() if len(parts) > 1 else root
            Visitor(lookup, rel, package, records).visit(tree)
    rev = git('rev-parse', 'HEAD')
    header = dict(schema_version=1, revision=rev.decode().strip() if rev else None, roots=roots, inputs=inputs, bytes=size, diagnostics=sorted(diagnostics, key=lambda d: (d['path'], d['reason'])), unsupported=[])
    output = {'header': header, 'records': [records[k] for k in sorted(records)]}
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / 'index.json').write_text(json.dumps(output, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    return int(any(d['reason'] in {'parse_error', 'missing_root'} for d in diagnostics))


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    ex = sub.add_parser('extract')
    ex.add_argument('--domain', required=True)
    ex.add_argument('--page', required=True)
    ex.add_argument('--root', action='append', required=True)
    ex.add_argument('--out', required=True)
    args = parser.parse_args()
    try:
        return extract(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'invalid input: {type(exc).__name__}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
