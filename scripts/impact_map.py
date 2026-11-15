#!/usr/bin/env python3
"""Extract literal candidates from Python source without importing it."""
import argparse
import ast
import json
import os
import re
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


def literals(resources, overrides=None):
    overrides = overrides or {}
    result = {}
    for resource in sorted(set(resources)):
        namespace, name = resource.split(':', 1)
        if not namespace or not name:
            raise ValueError('invalid resource')
        literal = name.rsplit('.', 1)[-1] if namespace in SUFFIX_NAMESPACES else name
        values = overrides.get(resource, [literal])
        if not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
            raise ValueError('invalid literals')
        for value in values:
            result.setdefault(value, []).append(resource)
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
        self.scopes = []

    def scope(self, node):
        self.stack.append(node.name)
        self.scopes.append(node)
        self.generic_visit(node)
        self.scopes.pop()
        self.stack.pop()

    visit_FunctionDef = scope
    visit_AsyncFunctionDef = scope
    visit_ClassDef = scope

    def visit_Assign(self, node):
        names = [n.id for target in node.targets for n in ast.walk(target) if isinstance(n, ast.Name)]
        direct_class = self.scopes and isinstance(self.scopes[-1], ast.ClassDef)
        if (not self.stack or direct_class) and names:
            for name in names:
                prefix = ['<module>', name] if not self.stack else [name]
                self.stack.extend(prefix)
                self.visit(node.value)
                del self.stack[-len(prefix):]
        else:
            self.generic_visit(node)

    def visit_AnnAssign(self, node):
        direct_class = self.scopes and isinstance(self.scopes[-1], ast.ClassDef)
        if direct_class and isinstance(node.target, ast.Name):
            self.add(node.target.id, 'attribute', node.lineno)
        if (not self.stack or direct_class) and isinstance(node.target, ast.Name):
            prefix = ['<module>', node.target.id] if not self.stack else [node.target.id]
            self.stack.extend(prefix)
            self.visit(node.annotation)
            if node.value is not None:
                self.visit(node.value)
            del self.stack[-len(prefix):]
        else:
            self.generic_visit(node)

    def add(self, literal, kind, line):
        for resource in self.lookup.get(literal, []):
            symbol = '.'.join(self.stack) or '<module>'
            key = (resource, self.path, symbol, kind)
            if key not in self.records or line < self.records[key]['reference_line']:
                self.records[key] = dict(resource=resource, path=self.path, symbol=symbol, package=self.package, match_kind=kind, reference_line=line)

    def visit_Constant(self, node):
        if isinstance(node.value, str):
            for literal in self.lookup:
                if node.value == literal:
                    self.add(literal, 'string_exact', node.lineno)
                elif re.search(r'(?<!\w)' + re.escape(literal) + r'(?!\w)', node.value):
                    self.add(literal, 'string_token', node.lineno)

    def visit_Attribute(self, node):
        self.add(node.attr, 'attribute', node.lineno)
        self.generic_visit(node)

    def visit_keyword(self, node):
        if node.arg is not None:
            self.add(node.arg, 'keyword', node.lineno)
        self.generic_visit(node)


def extract(args):
    page = front_matter(args.page)
    lookup = literals(page['resources'], page.get('literals'))
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


def read_covers(path):
    text = Path(path).read_text(encoding='utf-8')
    start, end = '<!-- impact-map:covers -->', '<!-- /impact-map:covers -->'
    if text.count(start) != 1 or text.count(end) != 1:
        raise ValueError('invalid covers markers')
    if text.index(end) < text.index(start):
        raise ValueError('invalid covers markers')
    body = text.split(start, 1)[1].split(end, 1)[0]
    match = re.fullmatch(r'\s*```json\s*\n(.*?)\n```\s*', body, re.DOTALL)
    if not match:
        raise ValueError('invalid covers block')
    covers = json.loads(match.group(1))['covers']
    for cover in covers:
        if (cover['kind'] not in ('page', 'manual_edge') or not cover['ids']
                or not isinstance(cover['ids'], list) or not all(isinstance(i, str) and i for i in cover['ids'])):
            raise ValueError('invalid cover')
        key(cover)
    return covers


def key(item):
    resource, symbol = item['resource'], item['symbol']
    if not isinstance(resource, str) or not isinstance(symbol, str) or not resource or '::' not in symbol:
        raise ValueError('invalid key')
    return resource, symbol


def evaluate(args):
    index = json.loads(Path(args.index).read_text(encoding='utf-8'))
    oracle = json.loads(Path(args.oracle).read_text(encoding='utf-8'))
    covers = read_covers(args.page)
    rows = oracle['rows']
    for row in rows:
        key(row)
        if row['class'] not in ('G_lit', 'G_reach') or type(row['critical']) is not bool:
            raise ValueError('invalid row')
        row['relation'], row['rationale']
    index_keys = {(r['resource'], r['path'] + '::' + r['symbol']) for r in index['records']}
    cover_map = {}
    for cover in covers:
        cover_map.setdefault(key(cover), []).append(cover)
    for values in cover_map.values():
        values.sort(key=lambda c: (c['kind'], tuple(c['ids'])))
    diagnostics = index['header']['diagnostics']
    unsupported = index['header']['unsupported']
    unresolved = oracle['header'].get('unresolved', [])
    for item in unresolved:
        key(item)
        if item['cell'] not in ('machine', 'page') or not item['reason']:
            raise ValueError('invalid unresolved')
    unresolved_by_cell = {cell: {key(u) for u in unresolved if u['cell'] == cell} for cell in ('machine', 'page')}
    def unscanned(k):
        path = k[1].split('::', 1)[0]
        return any(d['reason'] in ('parse_error', 'missing_root', 'symlink_outside_root') and
                   (path == d['path'] or path.startswith(d['path'].rstrip('/') + '/')) for d in diagnostics)
    def matches(expected, candidate):
        return expected[0] == candidate[0] and (candidate[1] == expected[1] or candidate[1].startswith(expected[1] + '.'))

    def cell(name, subset, candidates):
        expected = {key(r) for r in subset}
        critical = {key(r) for r in subset if r['critical']}
        if name == 'machine':
            found = {e for e in expected if any(matches(e, c) for c in candidates)}
            matched_candidates = {c for c in candidates if any(matches(e, c) for e in expected)}
        else:
            found = expected & candidates
            matched_candidates = found
        missed = expected - found
        unscanned_keys = {k for k in expected if unscanned(k)} if name == 'machine' else set()
        unknown = (unresolved_by_cell[name] & expected) | unscanned_keys
        false_positives = candidates - matched_candidates
        def counts(scope):
            return {'population': len(expected & scope), 'tp': len(found & scope),
                    'fn': len(missed & scope), 'u': len(unknown & scope),
                    'unscanned': len(unscanned_keys & scope)}
        result = counts(expected)
        result['false_negatives'] = [{'resource': r, 'symbol': s} for r, s in sorted(missed)]
        result['unresolved'] = []
        for resource, symbol in sorted(unknown):
            reasons = sorted({u['reason'] for u in unresolved if u['cell'] == name and key(u) == (resource, symbol)})
            path = symbol.split('::', 1)[0]
            if name == 'machine':
                reasons += sorted({d['reason'] for d in diagnostics if d['reason'] in ('parse_error', 'missing_root', 'symlink_outside_root') and (path == d['path'] or path.startswith(d['path'].rstrip('/') + '/'))})
            result['unresolved'].append({'resource': resource, 'symbol': symbol, 'reasons': sorted(set(reasons))})
        result['fp'] = len(false_positives)
        result['false_positives'] = [{'resource': r, 'symbol': s} for r, s in sorted(false_positives)]
        result['critical'] = counts(critical)
        result['critical']['fp'] = None
        result['unsupported'] = not expected or (name == 'machine' and bool(unsupported))
        result['precision'] = len(matched_candidates) / len(candidates) if candidates else None
        result['recall'] = len(found) / len(expected) if expected else None
        return result
    machine = cell('machine', [r for r in rows if r['class'] == 'G_lit'], index_keys)
    page = cell('page', rows, set(cover_map))
    classifications = []
    for row in sorted(rows, key=lambda r: (key(r), r['class'], r['relation'], r['rationale'])):
        k = key(row)
        classification = 'index_found' if any(matches(k, c) for c in index_keys) else 'page_manual' if k in cover_map else 'both_missed'
        classifications.append({**row, 'classification': classification, 'covers': cover_map.get(k, [])})
    passed = all(not c['unsupported'] and c['critical']['fn'] == 0 and c['critical']['u'] == 0 for c in (machine, page))
    result = {'domain': args.domain, 'machine': machine, 'page': page, 'rows': classifications,
              'needs_human_review': True, 'pass_candidate': passed}
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / 'metrics.json').write_text(json.dumps(result, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    return 0


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    ex = sub.add_parser('extract')
    ex.add_argument('--domain', required=True)
    ex.add_argument('--page', required=True)
    ex.add_argument('--root', action='append', required=True)
    ex.add_argument('--out', required=True)
    ev = sub.add_parser('evaluate')
    for option in ('domain', 'index', 'oracle', 'page', 'out'):
        ev.add_argument('--' + option, required=True)
    args = parser.parse_args()
    try:
        return extract(args) if args.command == 'extract' else evaluate(args)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print(f'invalid input: {type(exc).__name__}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
