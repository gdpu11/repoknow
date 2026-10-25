import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.engine import dependencies, overview
from app.main import create_app
from app.scanner import words
from test_core import FakeGit


class ExplorationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.app = create_app(temp.name, start_worker=False)
        self.store, self.engine = self.app.state.store, self.app.state.engine
        self.git = FakeGit(self.engine.stop_event)
        self.engine.git = self.git
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)
        self.client.headers['X-Requested-With'] = 'RepoKnow'
        account = json.loads((self.store.root / 'admin.json').read_text())
        self.client.post('/api/auth/login', json={k: account[k] for k in ('username', 'password')})
        self.headers = {'Authorization': 'Bearer ' + account['read_only_api_key']}

    def index(self):
        response = self.client.post('/api/repositories', json={'url': 'https://gitlab.example.com/team/example'})
        self.assertEqual(response.status_code, 201)
        self.repo_id = response.json()['id']
        self.assertTrue(self.engine.tick())
        return self.client.get('/api/repositories/' + self.repo_id).json()

    def tool(self, name, **arguments):
        response = self.client.post('/mcp', headers=self.headers, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': name, 'arguments': {'repository_id': self.repo_id, **arguments}}})
        self.assertEqual(response.status_code, 200)
        return response.json()['result']

    def data(self, name, **arguments):
        result = self.tool(name, **arguments)
        self.assertNotIn('isError', result)
        return json.loads(result['content'][0]['text'])

    def test_single_han_recall_does_not_broaden_multi_character_queries_to_unigrams(self):
        self.git.content = {'docs/storage.md': '# 数据库\n读写锁保护树形目录，数据表生成关系图。',
                            'docs/unrelated.md': '# 数字\n这个数目仅供统计。',
                            'auth.py': 'def login():\n    return "authentication"\n'}
        self.index()
        for query in ('锁', '树', '表', '图', '库', '数据库'):
            hits = self.data('knowledge_search', q=query)['results']
            self.assertTrue(hits, query)
            self.assertTrue(all(h['path'] == 'docs/storage.md' for h in hits), query)
        self.assertTrue(self.data('knowledge_search', q='认证')['results'])
        self.assertEqual(self.data('knowledge_search', q='???')['truncated'], False)

    def test_phrase_ranking_happens_before_candidate_limit_and_scopes_are_literal(self):
        self.git.content = {f'docs/noise-{i}.md': '# connection\nother words separate the pool' for i in range(350)}
        self.git.content.update({'docs/exact.md': '# Guide\n' + 'filler ' * 200 + 'connection pool',
                                 'pkg/api/handler.py': 'def common():\n    return "connection pool"\n',
                                 'docs/%_literal.md': 'connection pool', 'docs/XXliteral.md': 'connection pool'})
        self.index()
        data = self.data('knowledge_search', q='CONNECTION POOL', kind='document')
        self.assertIn(data['results'][0]['path'], {'docs/exact.md', 'docs/%_literal.md', 'docs/XXliteral.md'})
        self.assertIn('docs/exact.md', {r['path'] for r in data['results']})
        self.assertTrue(data['truncated'])
        scoped = self.data('knowledge_search', q='connection', path_prefix='pkg/api/', language='python')['results']
        self.assertEqual({r['path'] for r in scoped}, {'pkg/api/handler.py'})
        self.assertEqual({r['language'] for r in scoped}, {'Python'})
        literal = self.data('knowledge_search', q='connection', path_prefix='docs/%_')['results']
        self.assertEqual({r['path'] for r in literal}, {'docs/%_literal.md'})
        web = self.client.get('/api/search', params={'q': 'connection', 'path_prefix': 'pkg/api/', 'language': 'Python'})
        self.assertEqual(web.json()['results'], scoped)

    def test_legacy_tokenization_rebuilds_on_explicit_sync_even_at_same_commit(self):
        self.git.content = {'README.md': '# 读写锁与知识库'}
        with patch('app.engine.SCANNER_VERSION', '2'), patch('app.scanner.words',
                side_effect=lambda text, han_unigrams=True: words(text, han_unigrams=False)):
            self.index()
        self.assertEqual(self.data('knowledge_search', q='锁')['results'], [])
        old = self.client.get('/api/repositories/' + self.repo_id).json()['current_snapshot']
        self.client.post('/api/repositories/' + self.repo_id + '/sync')
        self.assertTrue(self.engine.tick())
        current = self.client.get('/api/repositories/' + self.repo_id).json()
        self.assertNotEqual(current['current_snapshot'], old)
        self.assertEqual(current['commit_id'], 'a' * 40)
        self.assertTrue(self.data('knowledge_search', q='锁')['results'])
        self.assertEqual(self.git.reads, 2)
        self.client.post('/api/repositories/' + self.repo_id + '/sync')
        self.engine.tick()
        self.assertEqual(self.git.reads, 2)

    def test_unicode_phrase_priority_is_consistent_before_and_after_candidate_limit(self):
        self.git.content = {f'docs/noise-{i}.md': '# chec\nother words separate connexion' for i in range(350)}
        self.git.content['docs/actual.md'] = '# Guide\n' + 'filler ' * 200 + 'Échec connexion'
        self.index()
        hits = self.data('knowledge_search', q='Échec connexion')['results']
        self.assertTrue(hits)
        self.assertEqual(hits[0]['path'], 'docs/actual.md')

    def test_overview_is_source_backed_and_does_not_return_file_bodies(self):
        self.git.content.update({'pyproject.toml': '[project]\ndependencies = ["example-lib>=2"]\n',
                                 'Cargo.toml': '[dependencies]\nserde = "1"\n',
                                 'pkg/component.py': 'def component():\n    return "fixture_body_not_in_overview"\n'})
        repo = self.index()
        data = self.data('knowledge_get_overview')
        self.assertEqual(data['commit_id'], repo['commit_id'])
        self.assertEqual(data['overview'], repo['overview'])
        self.assertIn('main.py', data['overview']['entrypoints'])
        self.assertIn('example-lib', {d['name'] for d in data['overview']['dependencies']})
        self.assertNotIn('fixture_body_not_in_overview', json.dumps(data))
        self.assertNotIn('credential', json.dumps(data))

    def test_directory_pages_are_direct_children_and_only_include_indexed_files(self):
        self.git.content.update({'src/a.py': 'a = 1', 'src/nested/b.py': 'b = 2',
                                 'docs/guide.md': '# Guide', '%_/literal.py': 'value = 1'})
        repo = self.index()
        entries, offset = [], 0
        while True:
            page = self.data('knowledge_list_files', commit=repo['commit_id'], limit=2, offset=offset)
            entries.extend(page['entries'])
            if page['next_offset'] is None:
                self.assertFalse(page['truncated'])
                break
            self.assertTrue(page['truncated'])
            offset = page['next_offset']
        self.assertEqual([p['path'] for p in entries], ['%_/', 'docs/', 'src/', 'README.md', 'main.py'])
        self.assertEqual(page['total'], len(entries))
        src = self.data('knowledge_list_files', path='src/')['entries']
        self.assertEqual([p['path'] for p in src], ['src/nested/', 'src/a.py'])
        self.assertEqual(src[1]['language'], 'Python')
        self.assertNotIn('content', src[1])
        literal = self.data('knowledge_list_files', path='%_')['entries']
        self.assertEqual([p['path'] for p in literal], ['%_/literal.py'])
        self.assertEqual(self.data('knowledge_list_files', path='missing')['entries'], [])
        for path in ('/', '/src', '../src', 'src/../docs', 'src\\nested', 'src//nested'):
            self.assertTrue(self.tool('knowledge_list_files', path=path)['isError'], path)

    def test_file_outline_is_paginated_and_exact_page_boundary_is_not_truncated(self):
        self.git.content['other.py'] = 'def helper():\n    return "different file"\n'
        repo = self.index()
        first = self.data('knowledge_get_symbols', path='main.py', limit=1, commit=repo['commit_id'])
        second = self.data('knowledge_get_symbols', path='main.py', limit=1,
                           offset=first['next_offset'], commit=repo['commit_id'])
        self.assertTrue(first['truncated'])
        self.assertFalse(second['truncated'])
        self.assertIsNone(second['next_offset'])
        self.assertEqual({s['name'] for p in (first, second) for s in p['symbols']}, {'helper', 'main'})
        self.assertTrue(all(s['path'] == 'main.py' for p in (first, second) for s in p['symbols']))
        web = self.client.get('/api/repositories/' + self.repo_id + '/symbols', params={'path': 'main.py', 'limit': 1})
        self.assertEqual(web.json(), first)

    def test_exploration_rejects_stale_versions_and_invalid_budgets(self):
        self.index()
        commit = self.data('knowledge_list_files')['commit_id']
        self.git.commit = 'b' * 40
        self.client.post('/api/repositories/' + self.repo_id + '/sync')
        self.engine.tick()
        for tool in ('knowledge_get_overview', 'knowledge_list_files', 'knowledge_get_symbols'):
            self.assertTrue(self.tool(tool, commit=commit)['isError'])
            self.assertEqual(self.data(tool)['commit_id'], 'b' * 40)
        for tool in ('knowledge_list_files', 'knowledge_get_symbols'):
            for arguments in ({'offset': -1}, {'offset': 2**100}, {'limit': 0}, {'limit': 301}):
                self.assertTrue(self.tool(tool, **arguments)['isError'])


class ManifestTests(unittest.TestCase):
    def test_python_project_optional_uv_groups_and_poetry_dependencies(self):
        content = '''[project]
dependencies = ["requests[security]>=2; python_version >= '3.10'"]
[project.optional-dependencies]
test = ["pytest>=8"]
[dependency-groups]
dev = ["ruff>=1", {include-group = "lint"}]
lint = ["mypy>=2"]
[tool.poetry.dependencies]
python = ">=3.10"
django = {version = "^5", optional = true}
[tool.poetry.group.test.dependencies]
coverage = "^7"
'''
        result = dependencies([{'path': 'pyproject.toml', 'content': content}])
        self.assertEqual({r['name'] for r in result}, {'requests', 'pytest', 'ruff', 'mypy', 'django', 'coverage'})
        self.assertEqual(next(r['version'] for r in result if r['name'] == 'django'), '^5')
        self.assertTrue(all(r['path'] == 'pyproject.toml' for r in result))

    def test_rust_workspace_target_and_source_dependencies_without_resolution(self):
        content = '''[dependencies]
serde = "1"
local = {path = "../local"}
shared = {workspace = true}
[workspace.dependencies]
tokio = {version = "1", features = ["full"]}
[target.'cfg(unix)'.build-dependencies]
cc = "1"
'''
        result = dependencies([{'path': 'Cargo.toml', 'content': content}])
        self.assertEqual({r['name']: r['version'] for r in result},
                         {'serde': '1', 'local': 'path', 'shared': 'workspace', 'tokio': '1', 'cc': '1'})

    def test_invalid_manifests_do_not_break_overview_and_limits_are_reported(self):
        files = [{'path': 'pyproject.toml', 'content': '[project\ninvalid'},
                 {'path': 'nested/Cargo.toml', 'content': '[dependencies]\ninvalid'},
                 {'path': 'package.json', 'content': json.dumps({'dependencies': {f'lib{i}': '1' for i in range(105)}})}]
        result = overview(files)
        self.assertEqual(len(result['dependencies']), 100)
        self.assertTrue(result['truncated']['dependencies'])
        self.assertFalse(result['truncated']['modules'])


if __name__ == '__main__':
    unittest.main()
