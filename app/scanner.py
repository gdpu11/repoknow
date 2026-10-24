"""CPU-only syntax scanning and source-backed navigation pages."""
from collections import Counter, defaultdict
import hashlib
from pathlib import PurePosixPath
import re

from tree_sitter import Language, Parser
import tree_sitter_go
import tree_sitter_java
import tree_sitter_javascript
import tree_sitter_python
import tree_sitter_typescript


GRAMMARS = {
    'Python': tree_sitter_python.language,
    'Go': tree_sitter_go.language,
    'JavaScript': tree_sitter_javascript.language,
    'TypeScript': tree_sitter_typescript.language_typescript,
    'TSX': tree_sitter_typescript.language_tsx,
    'Java': tree_sitter_java.language,
}
EXTENSIONS = {
    '.py': 'Python', '.go': 'Go', '.js': 'JavaScript', '.jsx': 'JavaScript',
    '.mjs': 'JavaScript', '.cjs': 'JavaScript', '.ts': 'TypeScript', '.tsx': 'TSX',
    '.java': 'Java', '.proto': 'Protobuf', '.sql': 'SQL', '.rs': 'Rust',
    '.c': 'C', '.cpp': 'C++', '.h': 'C/C++', '.cs': 'C#', '.kt': 'Kotlin',
    '.swift': 'Swift', '.rb': 'Ruby', '.php': 'PHP', '.sh': 'Shell',
    '.md': 'Markdown', '.mdx': 'Markdown', '.rst': 'reStructuredText',
    '.txt': 'Text', '.adoc': 'AsciiDoc', '.json': 'JSON', '.yaml': 'YAML',
    '.yml': 'YAML', '.toml': 'TOML', '.xml': 'XML', '.ini': 'INI',
    '.cfg': 'Config', '.html': 'HTML', '.css': 'CSS', '.vue': 'Vue',
}
DOCUMENT_LANGUAGES = {'Markdown', 'reStructuredText', 'Text', 'AsciiDoc'}
IGNORED_DIRS = {'.git', 'node_modules', 'vendor', '.venv', 'venv', '__pycache__',
                'dist', 'build', 'target', '.next', '.idea', '.vscode'}
SECRET = re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----|\bAKIA[A-Z0-9]{16}\b|\b(?:ghp_|glpat-)[A-Za-z0-9_-]{20,}')
DEFINITIONS = {
    'function_definition': 'function', 'function_declaration': 'function',
    'method_definition': 'method', 'method_declaration': 'method',
    'class_definition': 'class', 'class_declaration': 'class',
    'interface_declaration': 'interface', 'type_spec': 'type',
}
CALLS = {'call', 'call_expression', 'method_invocation'}


def words(text):
    # Split identifiers and include Han bigrams so Chinese text is searchable
    # with SQLite's built-in tokenizer; no embedding model is involved.
    text = re.sub(r'([a-z0-9])([A-Z])', r'\1 \2', text)
    values = re.findall(r'[a-zA-Z0-9_]+|[\u3400-\u9fff]+', text.lower())
    result = []
    for value in values:
        if re.match(r'[\u3400-\u9fff]', value):
            result.extend(value[i:i + 2] for i in range(max(1, len(value) - 1)))
        else:
            result.append(value)
    return result


def query_terms(query):
    aliases = {'接口': 'api route endpoint rpc', '配置': 'config settings options',
               '启动': 'main start run entry', '依赖': 'dependencies requires import',
               '认证': 'auth login authentication', '数据库': 'database sql model',
               '测试': 'test spec', '部署': 'docker deploy ci', '文档': 'readme docs'}
    for key, value in aliases.items():
        if key in query:
            query += ' ' + value
    stop = {'这个', '项目', '如何', '怎么', '哪里', '什么', '哪些', '代码', '一个', '的'}
    return list(dict.fromkeys(term for term in words(query) if term not in stop))[:24]


def eligible(entry):
    path = PurePosixPath(entry['path'])
    lower = path.name.lower()
    if entry['mode'] not in ('100644', '100755') or entry['type'] != 'blob':
        return None, 'symlink_or_submodule'
    if any(part in IGNORED_DIRS for part in path.parts):
        return None, 'excluded_directory'
    if (lower.startswith('.env') or lower in {'id_rsa', 'id_ed25519', 'credentials.json'}
            or lower.startswith('secrets.') or path.suffix.lower() in {'.pem', '.key', '.p12', '.pfx'}):
        return None, 'sensitive_file'
    if entry['size'] > 512 * 1024:
        return None, 'large_file'
    if lower.endswith(('.min.js', '.min.css', '.map', '.pb.go', '-lock.json', '.lock')):
        return None, 'generated_file'
    language = EXTENSIONS.get(path.suffix.lower())
    if lower in {'readme', 'license', 'copying', 'authors', 'changelog'}:
        language = 'Text'
    if lower in {'dockerfile', 'makefile', 'go.mod', 'go.sum', '.gitignore', '.gitlab-ci.yml', 'procfile'}:
        language = 'Config'
    return (language, '') if language else (None, 'unsupported_or_binary')


def parse_symbols(snapshot, path, language, content, diagnostics=None):
    if language not in GRAMMARS:
        return [], []
    raw = content.encode()
    parser = Parser(Language(GRAMMARS[language]()))
    root = parser.parse(raw).root_node
    if root.has_error and diagnostics is not None:
        diagnostics.append(path)
    symbols, calls = [], []
    stack = [(root, None, [])]
    while stack:
        node, owner, parents = stack.pop()
        name_node = node.child_by_field_name('name')
        kind = DEFINITIONS.get(node.type)
        if node.type == 'variable_declarator':
            value = node.child_by_field_name('value')
            if value and value.type in ('arrow_function', 'function_expression'):
                kind = 'function'
        current_owner, current_parents = owner, parents
        if kind and name_node:
            name = raw[name_node.start_byte:name_node.end_byte].decode(errors='replace')
            prefix = parents
            receiver = node.child_by_field_name('receiver') if language == 'Go' else None
            if receiver and receiver.named_children:
                receiver_type = receiver.named_children[0].child_by_field_name('type')
                if receiver_type:
                    value = raw[receiver_type.start_byte:receiver_type.end_byte].decode(errors='replace')
                    receiver_names = re.findall(r'[A-Za-z_]\w*', value)
                    if receiver_names:
                        prefix = parents + [receiver_names[0]]
            qualified = '.'.join(prefix + [name])
            start, end = node.start_point.row + 1, node.end_point.row + 1
            uid = hashlib.sha256(f'{snapshot}:{path}:{start}:{qualified}'.encode()).hexdigest()[:32]
            signature = content.splitlines()[start - 1].strip()[:300]
            symbol = {'id': uid, 'snapshot_id': snapshot, 'path': path, 'name': name,
                      'qualified_name': qualified, 'kind': kind, 'start_line': start,
                      'end_line': end, 'signature': signature}
            symbols.append(symbol)
            current_parents = prefix + [name]
            if kind in ('function', 'method'):
                current_owner = uid
        if node.type in CALLS and current_owner:
            callee = node.child_by_field_name('function') or node.child_by_field_name('name')
            if callee:
                target = raw[callee.start_byte:callee.end_byte].decode(errors='replace')
                simple = re.search(r'([A-Za-z_][\w]*)$', target)
                if simple:
                    calls.append({'source': current_owner, 'name': simple[1],
                                  'path': path, 'line': node.start_point.row + 1,
                                  'simple': '.' not in target and '::' not in target})
        for child in reversed(node.named_children):
            stack.append((child, current_owner, current_parents))
    return symbols, calls


def link_calls(symbols, calls):
    by_name, by_file = defaultdict(list), defaultdict(list)
    for symbol in symbols:
        if symbol['kind'] in ('function', 'method'):
            by_name[symbol['name']].append(symbol)
            by_file[(symbol['path'], symbol['name'])].append(symbol)
    seen, relations = set(), []
    for call in calls:
        candidates = by_file[(call['path'], call['name'])]
        same_file = bool(candidates)
        if not candidates:
            candidates = by_name[call['name']]
        if len(candidates) != 1:
            continue
        target = candidates[0]
        key = (call['source'], target['id'], call['line'])
        if key in seen:
            continue
        seen.add(key)
        relations.append({'source_id': call['source'], 'target_id': target['id'],
                          'kind': 'calls', 'confidence': 0.85 if same_file and call['simple'] else 0.55,
                          'path': call['path'], 'line': call['line']})
    return relations


def chunks_for(file, symbols):
    lines, chunks = file['content'].splitlines(), []
    for start in range(0, max(1, len(lines)), 80):
        end = min(start + 100, len(lines))
        chunks.append({'path': file['path'], 'start_line': start + 1, 'end_line': max(start + 1, end),
                       'kind': file['kind'], 'name': file['path'], 'text': '\n'.join(lines[start:end])})
    for symbol in symbols:
        start, end = symbol['start_line'], min(symbol['end_line'], symbol['start_line'] + 199)
        chunks.append({'path': file['path'], 'start_line': start, 'end_line': end,
                       'kind': 'symbol', 'name': symbol['qualified_name'],
                       'text': '\n'.join(lines[start - 1:end])})
    for chunk in chunks:
        chunk['search_text'] = ' '.join(words(chunk['name'] + ' ' + chunk['path'] + ' ' + chunk['text']))
    return chunks


def scan(source, directory, commit, snapshot):
    entries = source.files(directory, commit)
    skipped, selected = Counter(), []
    for entry in sorted(entries, key=lambda item: (not item['path'].lower().startswith(('readme', 'docs/', 'doc/')), item['path'])):
        language, reason = eligible(entry)
        if not language:
            skipped[reason] += 1
            continue
        selected.append(entry | {'language': language})
    if len(selected) > 20000 or sum(item['size'] for item in selected) > 64 * 1024 * 1024:
        raise ValueError('仓库超过本版扫描上限（2 万文件 / 64 MiB 文本），原索引保留')
    files, symbols, calls, chunks, diagnostics = [], [], [], [], []
    for entry, raw in source.blobs(directory, selected):
        if source.stop_event.is_set():
            raise RuntimeError('索引已取消')
        try:
            content = raw.decode('utf-8')
        except UnicodeDecodeError:
            skipped['non_utf8'] += 1
            continue
        if '\0' in content:
            skipped['binary'] += 1
            continue
        if SECRET.search(content):
            skipped['secret_pattern'] += 1
            continue
        kind = 'document' if entry['language'] in DOCUMENT_LANGUAGES else 'code'
        file = {key: entry[key] for key in ('path', 'sha', 'language')}
        file.update(content=content, kind=kind, line_count=max(1, len(content.splitlines())))
        entities, edges = parse_symbols(snapshot, file['path'], file['language'], content, diagnostics)
        files.append(file)
        symbols.extend(entities)
        calls.extend(edges)
        chunks.extend(chunks_for(file, entities))
    relations = link_calls(symbols, calls)
    statistics = {'files': len(files), 'documents': sum(f['kind'] == 'document' for f in files),
                  'symbols': len(symbols), 'relations': len(relations), 'chunks': len(chunks),
                  'skipped': sum(skipped.values()), 'skipped_reasons': dict(skipped),
                  'languages': dict(Counter(f['language'] for f in files)),
                  'syntax_error_files': diagnostics,
                  'parsed_languages': sorted(set(f['language'] for f in files) & GRAMMARS.keys())}
    return files, symbols, relations, chunks, statistics
