#!/usr/bin/env python3
"""Extract literal candidates from Python source without importing it."""
import hashlib
from datetime import datetime, timezone
import argparse
import ast
import json
import os
import re
from pathlib import Path
import subprocess
import sys

EXCLUDED = {'data', 'logs', 'plugins', 'tmp', '.venv', '.git', 'config'}
SUFFIX_NAMESPACES = {'state', 'config'}
FATAL = {'parse_error', 'missing_root', 'excluded_root', 'symlink_root', 'root_outside_repo', 'git_unavailable', 'invalid_include'}


def git(*args):
    try:
        return subprocess.run(['git', *args], check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL).stdout
    except (OSError, subprocess.CalledProcessError):
        return None


def relative(path):
    return Path(os.path.relpath(path, Path.cwd())).as_posix()


def front_matter(path):
    return parse_front_matter(Path(path).read_text(encoding='utf-8'))


def parse_front_matter(text):
    lines = text.splitlines()
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


def validate_root(root, repo, diagnostics, *, source_file=False):
    # Inspect the original components before normalizing away "..".
    path = Path(root)
    path = path if path.is_absolute() else Path.cwd() / path
    normalized = Path(os.path.abspath(path))
    if not normalized.is_relative_to(repo):
        diagnostics.append({'path': relative(normalized), 'reason': 'root_outside_repo'})
        return None
    lexical_parts = path.relative_to(repo).parts if path.is_relative_to(repo) else normalized.relative_to(repo).parts
    current = repo
    for part in lexical_parts:
        current = current / part
        if current.is_symlink():
            diagnostics.append({'path': relative(path), 'reason': 'symlink_root'})
            return None
    if any(part in EXCLUDED for part in lexical_parts):
        diagnostics.append({'path': relative(normalized), 'reason': 'excluded_root'})
        return None
    if not (normalized.is_file() if source_file else normalized.is_dir()):
        diagnostics.append({'path': relative(normalized), 'reason': 'invalid_include' if source_file else 'missing_root'})
        return None
    return normalized


def candidates(root, diagnostics, tracked_set, includes, repo, seen):
    resolved = root.resolve()
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
            if path in seen:
                continue
            seen.add(path)
            if tracked_set is not None and path not in tracked_set and path not in includes:
                diagnostics.append({'path': path.relative_to(repo).as_posix(), 'reason': 'untracked_runtime'})
                continue
            yield path, root


def compile_patterns(lookup):
    return {literal: re.compile(r'(?<!\w)' + re.escape(literal) + r'(?!\w)') for literal in lookup}


class Visitor(ast.NodeVisitor):
    def __init__(self, lookup, path, package, records, patterns=None):
        self.lookup, self.path, self.package, self.records = lookup, path, package, records
        self.patterns = patterns if patterns is not None else compile_patterns(lookup)
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
        direct_class = self.scopes and isinstance(self.scopes[-1], ast.ClassDef)
        if self.stack and not direct_class:
            self.generic_visit(node)
            return
        for target in node.targets:
            base = target
            while isinstance(base, (ast.Subscript, ast.Attribute)):
                base = base.value
            name = base.id if isinstance(base, ast.Name) else None
            if isinstance(target, ast.Name):
                prefix = ['<module>', name] if not self.stack else [name]
            elif not self.stack and name and name.isupper():
                prefix = ['<module>', name]
            else:
                prefix = []
            self.stack.extend(prefix)
            self.visit(target)
            self.visit(node.value)
            if prefix:
                del self.stack[-len(prefix):]

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
                elif self.patterns[literal].search(node.value):
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
    patterns = compile_patterns(lookup)
    diagnostics, records = [], {}
    inputs = size = 0
    roots = sorted(set(relative(Path(r).absolute()) for r in args.root))
    repo_raw = git('rev-parse', '--show-toplevel')
    repo = Path(os.fsdecode(repo_raw).strip()).resolve() if repo_raw else Path.cwd().resolve()
    validated = [validate_root(root, repo, diagnostics) for root in args.root]
    includes = set()
    for value in args.include:
        path = validate_root(value, repo, diagnostics, source_file=True)
        if path is None:
            continue
        matching = [root for root in validated if root is not None and path.is_relative_to(root)]
        if (not matching or path.suffix != '.py' or path.name.startswith('.') or
                not any(not any((parent / '.git').exists() for parent in path.parents
                                    if parent != root and parent.is_relative_to(root))
                        for root in matching)):
            diagnostics.append({'path': relative(path), 'reason': 'invalid_include'})
        else:
            includes.add(path)
    tracked_sets = {}
    if not diagnostics:
        for root in set(validated):
            tracked = git('ls-files', '-z', '--', str(root)) if repo_raw else None
            if tracked is None and not args.allow_untracked_walk:
                diagnostics.append({'path': relative(root), 'reason': 'git_unavailable'})
            tracked_sets[root] = None if tracked is None else {Path(os.fsdecode(p)).absolute() for p in tracked.split(b'\0') if p}
    seen = set()
    for base in sorted(tracked_sets) if not diagnostics else []:
        root = relative(base)
        for path, base in candidates(base, diagnostics, tracked_sets[base], includes, repo, seen):
            rel = relative(path)
            try:
                data = path.read_bytes()
                inputs += 1
                size += len(data)
                tree = ast.parse(data, filename=rel)
                parts = path.relative_to(base).parts
                package = (Path(root) / parts[0]).as_posix() if len(parts) > 1 else root
                file_records = {}
                Visitor(lookup, rel, package, file_records, patterns).visit(tree)
                records.update(file_records)
            except (SyntaxError, UnicodeDecodeError, ValueError, OSError, RecursionError, MemoryError):
                diagnostics.append({'path': rel, 'reason': 'parse_error'})
                continue
    rev = git('rev-parse', 'HEAD')
    header = dict(schema_version=1, revision=rev.decode().strip() if rev else None, roots=roots, inputs=inputs, bytes=size, diagnostics=sorted(diagnostics, key=lambda d: (d['path'], d['reason'])), unsupported=[])
    output = {'header': header, 'records': [records[k] for k in sorted(records)]}
    index_path = Path(args.out) / 'index.json'
    if any(d['reason'] in FATAL for d in diagnostics):
        index_path.unlink(missing_ok=True)
        print(json.dumps({'header': header, 'records': []}, sort_keys=True), file=sys.stderr)
        return 1
    Path(args.out).mkdir(parents=True, exist_ok=True)
    (Path(args.out) / 'index.json').write_text(json.dumps(output, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    return 0


def read_covers(path):
    return parse_covers(Path(path).read_text(encoding='utf-8'))


def parse_covers(text):
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
    if any(d['reason'] in FATAL for d in index['header']['diagnostics']):
        raise ValueError('fatal index diagnostics')
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
        unscanned_keys = {k for k in expected if unscanned(k)} if name == 'machine' else set()
        unknown = ((unresolved_by_cell[name] & expected) | unscanned_keys) - found
        missed = expected - found - unknown
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


METHOD = f"python-{sys.version_info.major}.{sys.version_info.minor}:ast.dump(annotate_fields=True,include_attributes=False):v1"


def anchor_digest(anchor, data=None):
    path, symbol = anchor.split('::', 1)
    source = Path(path)
    if source.is_absolute() or '..' in source.parts or source.resolve().is_relative_to(Path.cwd().resolve()) is False:
        raise ValueError('invalid path')
    node = None
    try:
        tree = ast.parse(source.read_bytes() if data is None else data, filename=path)
        parts = symbol.split('.')
        body = tree.body
        if parts[0] == '<module>' and len(parts) == 2:
            for item in body:
                targets = item.targets if isinstance(item, ast.Assign) else [item.target] if isinstance(item, ast.AnnAssign) else []
                if any(isinstance(t, ast.Name) and t.id == parts[1] for t in targets):
                    node = item
        else:
            def scoped_definitions(nodes):
                for child in nodes:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        yield child
                    else:
                        yield from scoped_definitions(ast.iter_child_nodes(child))
            for index, part in enumerate(parts):
                if index == len(parts) - 1 and isinstance(node, ast.ClassDef):
                    assignments = []
                    for item in body:
                        targets = item.targets if isinstance(item, ast.Assign) else [item.target] if isinstance(item, ast.AnnAssign) else []
                        if any(isinstance(target, ast.Name) and target.id == part for target in targets):
                            assignments.append(item)
                    if assignments:
                        node = assignments[-1]
                        break
                matches = [n for n in scoped_definitions(body) if n.name == part]
                if len(matches) != 1:
                    node = None
                    break
                node = matches[0]
                body = node.body
    except FileNotFoundError:
        pass
    digest = hashlib.sha256(ast.dump(node, annotate_fields=True, include_attributes=False).encode()).hexdigest() if node else None
    return node, digest


def verify(args):
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.page):
        raise ValueError('invalid domain')
    node, digest = anchor_digest(args.anchor)
    record_path = Path('docs/impact-map/verification') / (args.page + '.json')
    records = json.loads(record_path.read_text()) if record_path.exists() else []
    previous = next ( (r for r in records if r['anchor'] == args.anchor), None)
    status = 'anchor_missing' if node is None else 'missing_record' if previous is None else 'method_mismatch' if previous['method'] != METHOD else 'fresh' if previous['hash'] == digest else 'stale'
    if args.record:
        if not digest or digest != args.expected_hash:
            raise ValueError('expected hash mismatch')
        for value in (args.reason, args.evidence):
            if not value or re.search(r'[\r\n]|[a-z][a-z0-9+.-]*://|/home|tmp/', value, re.I):
                raise ValueError('invalid public text')
        revision = git('rev-parse', 'HEAD')
        entry = dict(anchor=args.anchor, hash=digest, method=METHOD, date=datetime.now(timezone.utc).date().isoformat(), evidence=args.evidence, source_sha=revision.decode().strip() if revision else None, reason=args.reason)
        records = [r for r in records if r['anchor'] != args.anchor] + [entry]
        records.sort(key=lambda r: r['anchor'])
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(records, sort_keys=True, separators=(',', ':')) + '\n', encoding='utf-8')
    result = dict(current_hash=digest, recorded_hash=previous['hash'] if previous else None, status=status)
    if node and node.end_lineno - node.lineno + 1 > 300:
        result['coarse_symbol'] = args.anchor
    print(json.dumps(result, sort_keys=True))
    return 0



def check(args):
    items = set()
    def add(kind, anchor, reason):
        items.add((kind, anchor, reason))
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.page):
        add('declaration', 'page', 'invalid_domain')
    if not git('rev-parse', 'HEAD'):
        add('history', 'HEAD', 'git_unavailable')
    page = Path('docs/impact-map/domains') / (args.page + '.md')
    anchors = []
    try:
        if not re.fullmatch(r'[A-Za-z0-9_-]+', args.page):
            raise ValueError()
        meta = front_matter(page)
        if not isinstance(meta, dict):
            raise ValueError()
        for name in ('id', 'owner_role', 'resources', 'anchors', 'related', 'tests'):
            if name not in meta:
                add('required', name, 'missing_key')
        for name in ('anchors', 'related'):
            if name in meta and (not isinstance(meta[name], list) or not all(isinstance(v, str) and v for v in meta[name])):
                raise ValueError()
        for anchor in meta.get('anchors', []):
            anchors.append(('anchor', anchor))
        for ticket in meta.get('related', []):
            if not re.fullmatch(r'[A-Za-z0-9_-]+', ticket):
                add('declaration', 'related', 'invalid_ticket')
            elif not any((Path('docs/tickets') / state / (ticket + '.md')).is_file() for state in ('open', 'closed')):
                add('ticket', ticket, 'ticket_missing')
    except (OSError, ValueError, TypeError, KeyError):
        add('declaration', 'front_matter', 'invalid_front_matter')
    try:
        for cover in read_covers(page):
            anchors.append(('cover', cover['symbol']))
    except (OSError, ValueError, TypeError, KeyError, IndexError):
        add('declaration', 'covers', 'invalid_covers')
    try:
        text = page.read_text(encoding='utf-8')
        for match in re.finditer(r'^- \*\*E[0-9]+\*\* `([^`]+)` → `([^`]+)`', text, re.MULTILINE):
            anchors.extend(('edge_endpoint', value) for value in match.groups())
    except (OSError, UnicodeError):
        add('declaration', 'page', 'unreadable_page')
    record_path = Path('docs/impact-map/verification') / ((args.page if re.fullmatch(r'[A-Za-z0-9_-]+', args.page) else '_invalid_') + '.json')
    try:
        records = json.loads(record_path.read_text(encoding='utf-8')) if record_path.exists() else []
        if not isinstance(records, list):
            raise ValueError()
        for record in records:
            anchor, method, digest = record['anchor'], record['method'], record['hash']
            if not all(isinstance(x, str) and x for x in (anchor, method, digest)):
                raise ValueError()
            path_part = anchor.split('::', 1)[0]
            if '::' not in anchor or Path(path_part).is_absolute() or '..' in Path(path_part).parts:
                raise ValueError()
            anchors.append(('record', anchor))
            if method != METHOD:
                add('record', anchor, 'method_mismatch')
    except (OSError, ValueError, TypeError, KeyError):
        records = []
        add('declaration', 'verification', 'invalid_record')
    hashes = {}
    for kind, anchor in sorted(set(anchors)):
        try:
            path, symbol = anchor.split('::', 1)
            if not path or not symbol or Path(path).is_absolute() or '..' in Path(path).parts:
                raise ValueError()
            if anchor not in hashes:
                hashes[anchor] = anchor_digest(anchor)[1]
            if hashes[anchor] is None:
                add(kind, anchor, 'anchor_missing')
        except (ValueError, TypeError):
            add('declaration', kind, 'invalid_anchor')
        except (OSError, SyntaxError, UnicodeError, RecursionError, MemoryError):
            add('source', anchor, 'parse_error')
    for record in records:
        anchor = record['anchor']
        if record['method'] == METHOD and hashes.get(anchor) is not None and hashes[anchor] != record['hash']:
            add('record', anchor, 'stale')
    errors2 = {'git_unavailable', 'invalid_domain', 'invalid_front_matter', 'invalid_covers', 'invalid_ticket', 'invalid_record', 'invalid_anchor', 'unreadable_page', 'parse_error', 'method_mismatch'}
    reasons = ('stale', 'anchor_missing', 'missing_key', 'ticket_missing', *sorted(errors2))
    result = {'counts': {reason: sum(item[2] == reason for item in items) for reason in reasons},
              'items': [dict(kind=k, anchor=a, reason=r) for k, a, r in sorted(items)]}
    print(json.dumps(result, sort_keys=True, separators=(',', ':')))
    return 2 if any(r in errors2 for _, _, r in items) else 1 if items else 0



def affected(args):
    def fail(reason):
        print(json.dumps({'error': reason}, sort_keys=True))
        return 2
    base = git('rev-parse', '--verify', '--end-of-options', args.base + '^{commit}')
    if not base:
        return fail('base_unavailable')
    base = base.decode().strip()
    head = None
    if not args.worktree:
        raw = git('rev-parse', '--verify', '--end-of-options', args.head + '^{commit}')
        if not raw:
            return fail('head_unavailable')
        head = raw.decode().strip()
    if not re.fullmatch(r'[A-Za-z0-9_-]+', args.page):
        return fail('invalid_page')
    try:
        page = Path('docs/impact-map/domains') / (args.page + '.md')
        def page_data(rev):
            data = git('show', rev + ':' + page.as_posix()) if rev else page.read_bytes()
            if data is None:
                raise OSError('page unavailable')
            text = data.decode('utf-8')
            meta = parse_front_matter(text)
            declarations = set(meta['anchors']) | {c['symbol'] for c in parse_covers(text)}
            for m in re.finditer(r'^- \*\*E[0-9]+\*\* `([^`]+)` → `([^`]+)`', text, re.MULTILINE):
                declarations.update(m.groups())
            return meta, declarations
        _, old_declarations = page_data(base)
        meta, new_declarations = page_data(head)
        declarations = old_declarations | new_declarations
        lookup = literals(meta['resources'], meta.get('literals'))
        for anchor in declarations:
            path, symbol = anchor.split('::', 1)
            if not path or not symbol or Path(path).is_absolute() or '..' in Path(path).parts:
                raise ValueError('invalid anchor')
        def runtime(path):
            parts = Path(path).parts
            return (path.endswith('.py') and not Path(path).name.startswith('.') and
                    not any(p in EXCLUDED or p in ('tests', 'test', 'scripts', 'docs') for p in parts))
        def listing(rev):
            raw = git('ls-tree', '-r', '--name-only', '-z', rev) if rev else git('ls-files', '-z', '--cached', '--others', '--exclude-standard')
            if raw is None:
                raise OSError('listing failed')
            return {os.fsdecode(p) for p in raw.split(b'\0') if p and runtime(os.fsdecode(p))}
        old_files, new_files = listing(base), listing(head)
        if head is None:
            new_files = {p for p in new_files if Path(p).is_file() and not Path(p).is_symlink()}
        def read(rev, path, files):
            if path not in files:
                return None
            data = git('show', rev + ':' + path) if rev else Path(path).read_bytes()
            if data is None:
                raise OSError('source unavailable')
            return data
        # Declarations can name files outside the runtime scan.
        def declared_data(rev, path):
            if rev:
                raw = git('cat-file', '-e', rev + ':' + path)
                return git('show', rev + ':' + path) if raw is not None else None
            source = Path(path)
            return source.read_bytes() if source.is_file() else None
        items = set()
        for anchor in sorted(declarations):
            path = anchor.split('::', 1)[0]
            old, new = declared_data(base, path), declared_data(head, path)
            before = anchor_digest(anchor, old)[1] if old is not None else None
            after = anchor_digest(anchor, new)[1] if new is not None else None
            if before is not None and after is None:
                items.add(('unmapped_removed_dependency', anchor, '', ''))
            elif before != after:
                kind = 'dependency_changed' if '::<module>.' in anchor else 'symbol_changed'
                items.add((kind, anchor, '', ''))
        patterns = compile_patterns(lookup)
        for path in sorted(old_files | new_files):
            old, new = read(base, path, old_files), read(head, path, new_files)
            if old is None and new is not None:
                items.add(('unmapped_new_file', path, '', ''))
            if old == new:
                continue
            def extract_set(data):
                if data is None:
                    return set()
                records = {}
                Visitor(lookup, path, '', records, patterns).visit(ast.parse(data, filename=path))
                return {(r['resource'], r['path'] + '::' + r['symbol']) for r in records.values()}
            before, after = extract_set(old), extract_set(new)
            if old is not None and new is not None and after:
                def symbols(data):
                    tree = ast.parse(data, filename=path)
                    result = {}
                    def visit(body, prefix=''):
                        for node in body:
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                                name = prefix + node.name
                                result[name] = ast.dump(node, annotate_fields=True, include_attributes=False)
                                if isinstance(node, ast.ClassDef):
                                    visit(node.body, name + '.')
                            elif isinstance(node, (ast.Assign, ast.AnnAssign)) and not prefix:
                                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                                for target in targets:
                                    if isinstance(target, ast.Name):
                                        result['<module>.' + target.id] = ast.dump(node, annotate_fields=True, include_attributes=False)
                    visit(tree.body)
                    return result
                old_symbols, new_symbols = symbols(old), symbols(new)
                for symbol in old_symbols.keys() | new_symbols.keys():
                    anchor = path + '::' + symbol
                    if anchor not in declarations and old_symbols.get(symbol) != new_symbols.get(symbol):
                        items.add(('unmapped_changed_unmapped_symbol', anchor, '', ''))
            for resource, symbol in before ^ after:
                items.add(('literal_candidates_changed', symbol, resource, 'added' if (resource, symbol) in after else 'removed'))
        rows = []
        for kind, anchor, resource, change in sorted(items):
            row = {'kind': kind, 'anchor': anchor}
            if resource:
                row.update(resource=resource, change=change)
            rows.append(row)
        print(json.dumps({'items': rows}, sort_keys=True, separators=(',', ':')))
        return 0
    except (OSError, ValueError, KeyError, TypeError, SyntaxError, UnicodeError, RecursionError):
        return fail('analysis_failed')


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command', required=True)
    ex = sub.add_parser('extract')
    ex.add_argument('--domain', required=True)
    ex.add_argument('--page', required=True)
    ex.add_argument('--root', action='append', required=True)
    ex.add_argument('--out', required=True)
    ex.add_argument('--allow-untracked-walk', action='store_true')
    ex.add_argument('--include', action='append', default=[])
    ev = sub.add_parser('evaluate')
    for option in ('domain', 'index', 'oracle', 'page', 'out'):
        ev.add_argument('--' + option, required=True)
    ve = sub.add_parser('verify')
    ve.add_argument('--page', required=True)
    ve.add_argument('--anchor', required=True)
    ve.add_argument('--record', action='store_true')
    for option in ('expected-hash', 'reason', 'evidence'):
        ve.add_argument('--' + option)
    ck = sub.add_parser('check')
    ck.add_argument('--page', required=True)
    af = sub.add_parser('affected')
    af.add_argument('--page', required=True)
    af.add_argument('--base', required=True)
    target = af.add_mutually_exclusive_group(required=True)
    target.add_argument('--head')
    target.add_argument('--worktree', action='store_true')
    args = parser.parse_args()
    try:
        return extract(args) if args.command == 'extract' else evaluate(args) if args.command == 'evaluate' else verify(args) if args.command == 'verify' else check(args) if args.command == 'check' else affected(args)
    except (OSError, ValueError, KeyError, TypeError, SyntaxError) as exc:
        print(f'invalid input: {type(exc).__name__}', file=sys.stderr)
        return 2


if __name__ == '__main__':
    sys.exit(main())
