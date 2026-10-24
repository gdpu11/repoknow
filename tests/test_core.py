import hashlib
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.git_source import GitSource, normalize_url, source_url
from app.engine import Engine
from app.main import create_app
from app.scanner import parse_symbols, link_calls, scan


class FakeGit:
    def __init__(self, stop_event):
        self.stop_event = stop_event
        self.commit = 'a' * 40
        self.content = {
            'README.md': '# 示例项目\n\n启动配置和使用说明。\n',
            'main.py': 'def helper():\n    return "original_text"\n\ndef main():\n    return helper()\n',
            '.env': 'TOKEN=must_not_be_indexed',
            'secret.pem': 'private material',
        }
        self.reads = 0

    def fetch(self, repo, credentials):
        return '/unused', repo['branch'] or 'main', self.commit

    def files(self, directory, commit):
        return [{'path': path, 'mode': '100644', 'type': 'blob',
                 'sha': hashlib.sha1(text.encode()).hexdigest(), 'size': len(text.encode())}
                for path, text in self.content.items()]

    def blobs(self, directory, entries):
        self.reads += 1
        for entry in entries:
            yield entry, self.content[entry['path']].encode()


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, start_worker=False)
        self.store = self.app.state.store
        self.engine = self.app.state.engine
        self.git = FakeGit(self.engine.stop_event)
        self.engine.git = self.git
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.client.headers['X-Requested-With'] = 'RepoKnow'
        self.credentials = json.loads((self.store.root / 'admin.json').read_text())
        login = self.client.post('/api/auth/login', json={
            'username': self.credentials['username'], 'password': self.credentials['password']})
        self.assertEqual(login.status_code, 200)

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def add(self, **extra):
        response = self.client.post('/api/repositories', json={
            'url': 'https://gitlab.example.com/team/subgroup/project.git', **extra})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()['id']

    def indexed(self):
        repo_id = self.add()
        self.assertTrue(self.engine.tick())
        return repo_id

    def test_scan_docs_symbols_relations_and_provenance(self):
        repo_id = self.indexed()
        repo = self.client.get('/api/repositories/' + repo_id).json()
        self.assertEqual(repo['sync_state'], 'ready')
        self.assertEqual(repo['statistics']['files'], 2)
        self.assertEqual(repo['statistics']['symbols'], 2)
        self.assertEqual(repo['statistics']['relations'], 1)
        self.assertEqual(repo['statistics']['skipped_reasons']['sensitive_file'], 2)
        result = self.client.get('/api/search', params={'q': 'helper'}).json()['results']
        self.assertTrue(result)
        self.assertTrue(all(item['commit_id'] == 'a' * 40 for item in result))
        self.assertIn('/-/blob/' + 'a' * 40, result[0]['source_url'])
        self.assertIn('team/subgroup/project', result[0]['source_url'])
        chinese = self.client.get('/api/search', params={'q': '启动配置'}).json()['results']
        self.assertTrue(any(item['path'] == 'README.md' for item in chinese))
        self.assertEqual(self.client.get(f'/api/repositories/{repo_id}/file', params={'path': '.env'}).status_code, 404)

    def test_encrypted_credentials_never_returned(self):
        token = 'fixture-private-access-token-value'
        repo_id = self.add(username='example-user', token=token)
        with self.store.db() as con:
            raw = con.execute('SELECT credential FROM repositories WHERE id=?', (repo_id,)).fetchone()[0]
        self.assertNotIn(token.encode(), raw)
        self.assertEqual(self.store.decrypt(repo_id, raw)['token'], token)
        with self.assertRaisesRegex(ValueError, '解密失败'):
            self.store.decrypt('wrong-repository', raw)
        body = self.client.get('/api/repositories').text
        self.assertNotIn(token, body)
        self.assertNotIn('example-user', body)
        self.assertIn('has_credentials', body)

    def test_cross_user_permissions_and_search_isolation(self):
        repo_id = self.indexed()
        other = TestClient(self.app)
        other.headers['X-Requested-With'] = 'RepoKnow'
        self.assertEqual(self.client.post('/api/admin/users', json={'username': 'other-user', 'password': 'Other-pass-12345'}).status_code, 201)
        other.post('/api/auth/login', json={'username': 'other-user', 'password': 'Other-pass-12345'})
        self.assertEqual(other.get('/api/repositories').json(), [])
        self.assertEqual(other.get(f'/api/repositories/{repo_id}').status_code, 404)
        self.assertEqual(other.get('/api/search', params={'q': 'helper'}).json()['results'], [])
        self.assertEqual(other.post(f'/api/repositories/{repo_id}/sync', json={}).status_code, 404)
        self.assertEqual(other.put(f'/api/repositories/{repo_id}/credentials', json={'token': 'replacement'}).status_code, 404)
        other.close()

    def test_shared_repository_is_readable_but_not_mutable(self):
        repo_id = self.add(visibility='shared')
        self.engine.tick()
        other = TestClient(self.app)
        other.headers['X-Requested-With'] = 'RepoKnow'
        self.client.post('/api/admin/users', json={'username': 'reader', 'password': 'Reader-pass-12345'})
        other.post('/api/auth/login', json={'username': 'reader', 'password': 'Reader-pass-12345'})
        self.assertEqual(other.get(f'/api/repositories/{repo_id}').status_code, 200)
        self.assertTrue(other.get('/api/search', params={'q': 'helper'}).json()['results'])
        self.assertEqual(other.post(f'/api/repositories/{repo_id}/sync', json={}).status_code, 404)
        other.close()

    def test_failed_generation_keeps_old_snapshot_and_fts(self):
        repo_id = self.indexed()
        old = self.client.get('/api/repositories/' + repo_id).json()['current_snapshot']
        self.git.commit = 'b' * 40
        self.git.content['main.py'] = 'def replacement():\n    return "new_generation"\n'
        self.client.post(f'/api/repositories/{repo_id}/sync', json={})
        original = scan

        def invalid_relations(*args):
            result = original(*args)
            result[2].append({'source_id': result[1][0]['id'], 'target_id': 'nonexistent',
                              'kind': 'calls', 'confidence': 0.5, 'path': 'main.py', 'line': 1})
            return result

        with patch('app.engine.scan', side_effect=invalid_relations):
            self.engine.tick()
        repo = self.client.get('/api/repositories/' + repo_id).json()
        self.assertEqual(repo['sync_state'], 'failed')
        self.assertEqual(repo['current_snapshot'], old)
        self.assertEqual(repo['commit_id'], 'a' * 40)
        self.assertTrue(self.client.get('/api/search', params={'q': 'original_text'}).json()['results'])
        self.assertFalse(self.client.get('/api/search', params={'q': 'new_generation'}).json()['results'])

    def test_no_change_skips_parse_and_duplicate_sync_is_rejected(self):
        repo_id = self.indexed()
        old = self.client.get('/api/repositories/' + repo_id).json()['current_snapshot']
        self.assertEqual(self.client.post(f'/api/repositories/{repo_id}/sync', json={}).status_code, 200)
        self.assertEqual(self.client.post(f'/api/repositories/{repo_id}/sync', json={}).status_code, 409)
        self.engine.tick()
        self.assertEqual(self.git.reads, 1)
        self.assertEqual(self.client.get('/api/repositories/' + repo_id).json()['current_snapshot'], old)
        self.assertEqual(self.client.get(f'/api/repositories/{repo_id}/jobs').json()[0]['state'], 'unchanged')

    def test_markdown_does_not_execute_raw_html_or_script_links(self):
        self.git.content['README.md'] = '# Guide\n<script>alert(1)</script>\n[bad](javascript:alert(1))\n![tracking](https://example.com/pixel)'
        repo_id = self.indexed()
        html = self.client.get(f'/api/repositories/{repo_id}/file', params={'path': 'README.md'}).json()['html']
        self.assertNotIn('<script>', html)
        self.assertNotIn('href="javascript:', html)
        self.assertNotIn('<img', html)

    def test_csrf_and_read_only_api_token(self):
        response = self.client.post('/api/repositories', json={'url': 'https://github.com/a/b'},
                                    headers={'Origin': 'https://attacker.example'})
        self.assertEqual(response.status_code, 403)
        response = self.client.post('/api/repositories', json={'url': 'https://github.com/a/b'},
                                    headers={'X-Requested-With': ''})
        self.assertEqual(response.status_code, 403)
        token = self.credentials['read_only_api_key']
        headers = {'Authorization': 'Bearer ' + token}
        self.assertEqual(self.client.get('/api/repositories', headers=headers).status_code, 200)
        self.assertEqual(self.client.post('/api/repositories', json={'url': 'https://github.com/a/b'}, headers=headers).status_code, 403)
        tools = self.client.post('/mcp', json={'jsonrpc': '2.0', 'id': 1, 'method': 'tools/list'}, headers=headers).json()
        self.assertEqual(len(tools['result']['tools']), 5)

    def test_mcp_source_requires_exact_current_commit(self):
        repo_id = self.indexed()
        def read(commit):
            return self.client.post('/mcp', json={'jsonrpc': '2.0', 'id': 2, 'method': 'tools/call',
                'params': {'name': 'knowledge_get_source', 'arguments': {
                    'repository_id': repo_id, 'commit': commit, 'path': 'main.py', 'start_line': 1, 'end_line': 5}}}).json()['result']
        valid = read('a' * 40)
        self.assertNotIn('isError', valid)
        self.assertIn('helper', valid['content'][0]['text'])
        invalid = read('b' * 40)
        self.assertTrue(invalid['isError'])
        self.assertIn('不会回退', invalid['content'][0]['text'])

    def test_snapshot_reader_stays_consistent_during_publication(self):
        repo_id = self.indexed()
        with self.store.db() as reader:
            old = dict(reader.execute('SELECT * FROM repositories WHERE id=?', (repo_id,)).fetchone())
            self.git.commit = 'b' * 40
            self.git.content['main.py'] = 'def updated():\n    return "new-generation"\n'
            self.store.queue(repo_id)
            self.engine.tick()
            source = reader.execute('SELECT content FROM files WHERE snapshot_id=? AND path=?',
                                    (old['current_snapshot'], 'main.py')).fetchone()[0]
            self.assertIn('original_text', source)
        self.assertEqual(self.client.get('/api/repositories/' + repo_id).json()['commit_id'], 'b' * 40)

    def test_malformed_mcp_arguments_are_tool_errors(self):
        result = self.client.post('/mcp', json={'jsonrpc': '2.0', 'id': 9, 'method': 'tools/call',
            'params': {'name': 'knowledge_get_source', 'arguments': {'path': ['bad']}}})
        self.assertEqual(result.status_code, 200)
        self.assertTrue(result.json()['result']['isError'])

    def test_bad_url_does_not_reach_git(self):
        for url in ('file:///tmp/private', 'ssh://host/team/repo', 'https://token@example.com/a/b',
                    'https://example.com/a/b?token=secret', 'https://example.com/a/../b'):
            with self.subTest(url=url):
                self.assertEqual(self.client.post('/api/repositories', json={'url': url}).status_code, 422)

    def test_file_read_rejects_obsolete_commit(self):
        repo_id = self.indexed()
        endpoint = f'/api/repositories/{repo_id}/file'
        query = {'path': 'main.py', 'commit': 'a' * 40}
        self.assertEqual(self.client.get(endpoint, params=query).status_code, 200)
        self.git.commit = 'b' * 40
        self.git.content['main.py'] = 'def replacement():\n    return "new_generation"\n'
        self.store.queue(repo_id)
        self.engine.tick()
        stale = self.client.get(endpoint, params=query)
        self.assertEqual(stale.status_code, 409)
        self.assertNotIn('new_generation', stale.text)
        current = self.client.get(endpoint, params={'path': 'main.py', 'commit': 'b' * 40})
        self.assertEqual(current.json()['commit_id'], 'b' * 40)
        self.assertIn('new_generation', current.json()['content'])
        self.assertEqual(self.client.get(endpoint, params={'path': 'main.py', 'commit': 'short'}).status_code, 422)

    def test_body_size_limit_validates_actual_chunks(self):
        for length in ('not-a-number', '-1'):
            with self.subTest(length=length):
                response = self.client.post('/mcp', content=b'{}', headers={'Content-Length': length})
                self.assertEqual(response.status_code, 400)
        body = json.dumps({'jsonrpc': '2.0', 'id': 1, 'method': 'ping'}).encode()
        valid = self.client.post('/mcp', content=iter([body[:10], body[10:]]),
                                 headers={'Content-Type': 'application/json'})
        self.assertEqual(valid.json()['result'], {})
        for headers in ({}, {'Content-Length': '1'}, {'Content-Length': '80000'}):
            with self.subTest(headers=headers):
                response = self.client.post('/mcp', content=iter([b' ' * 40000, b' ' * 40000]),
                    headers={'Content-Type': 'application/json', **headers})
                self.assertEqual(response.status_code, 413)

    def test_shutdown_after_scan_keeps_previous_snapshot(self):
        repo_id = self.indexed()
        old = self.client.get('/api/repositories/' + repo_id).json()['current_snapshot']
        self.git.commit = 'b' * 40
        self.store.queue(repo_id)

        def interrupted_scan(*args):
            result = scan(*args)
            self.engine.stop_event.set()
            return result

        with patch('app.engine.scan', side_effect=interrupted_scan):
            self.engine.tick()
        repo = self.client.get('/api/repositories/' + repo_id).json()
        self.assertEqual(repo['current_snapshot'], old)
        self.assertEqual(repo['sync_state'], 'queued')
        self.assertEqual(self.client.get(f'/api/repositories/{repo_id}/jobs').json()[0]['state'], 'interrupted')
        self.engine.stop_event.clear()
        self.engine.tick()
        self.assertEqual(self.client.get('/api/repositories/' + repo_id).json()['commit_id'], 'b' * 40)

    def test_shutdown_holds_lock_until_worker_finishes(self):
        self.add()
        entered, release = threading.Event(), threading.Event()
        original = self.git.fetch

        def slow_fetch(*args):
            entered.set()
            if not release.wait(5):
                raise RuntimeError('test worker timed out')
            return original(*args)

        contender = Engine(self.store)
        contender.git = FakeGit(contender.stop_event)
        try:
            with patch.object(self.git, 'fetch', side_effect=slow_fetch):
                self.engine.start()
                self.assertTrue(entered.wait(3))
                # Simulate expiry of stop()'s grace period without a 10s test delay.
                with patch.object(self.engine.thread, 'join'):
                    self.engine.stop()
                with self.assertRaisesRegex(RuntimeError, '已有实例'):
                    contender.start()
                release.set()
                self.engine.thread.join(timeout=5)
                self.assertFalse(self.engine.thread.is_alive())
            contender.start()
        finally:
            release.set()
            self.engine.stop()
            contender.stop()


class ParserTests(unittest.TestCase):
    def test_supported_syntax_and_direct_call_links(self):
        cases = [
            ('Python', 'a.py', 'def helper():\n    return 1\ndef main():\n    return helper()\n'),
            ('Go', 'a.go', 'package main\nfunc helper() {}\nfunc main() { helper() }'),
            ('TypeScript', 'a.ts', 'function helper() { return 1; }\nconst main = () => helper();'),
            ('JavaScript', 'a.js', 'function helper() {}\nfunction main() { helper(); }'),
            ('Java', 'A.java', 'class A { void helper() {} void main() { helper(); } }'),
        ]
        for language, path, source in cases:
            with self.subTest(language=language):
                symbols, calls = parse_symbols('snapshot', path, language, source)
                self.assertIn('helper', [s['name'] for s in symbols])
                self.assertEqual(len(link_calls(symbols, calls)), 1)

    def test_ambiguous_call_is_not_linked(self):
        source = 'class A:\n    def helper(self): pass\nclass B:\n    def helper(self): pass\ndef main():\n    helper()\n'
        symbols, calls = parse_symbols('snapshot', 'a.py', 'Python', source)
        self.assertEqual(link_calls(symbols, calls), [])

    def test_go_method_qualified_name_and_parse_diagnostics(self):
        symbols, _ = parse_symbols('snapshot', 'mux.go', 'Go',
            'package main\ntype Mux struct {}\nfunc (mx *Mux) Serve() {}')
        self.assertIn('Mux.Serve', [symbol['qualified_name'] for symbol in symbols])
        diagnostics = []
        parse_symbols('snapshot', 'broken.py', 'Python', 'def broken(:\n pass', diagnostics)
        self.assertEqual(diagnostics, ['broken.py'])

    def test_git_credentials_are_not_command_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            source = GitSource(directory, threading.Event())
            fake = unittest.mock.Mock()
            fake.communicate.return_value = (b'ok', b'')
            fake.returncode = 0
            fake.poll.return_value = 0
            with patch('app.git_source.subprocess.Popen', return_value=fake) as popen:
                source.run(['ls-remote', 'https://gitlab.example.com/a/b.git'],
                           credentials={'username': 'alice', 'token': 'private-token'})
            args, kwargs = popen.call_args
            self.assertNotIn('private-token', str(args))
            self.assertEqual(kwargs['env']['REPOKNOW_GIT_TOKEN'], 'private-token')
            self.assertNotIn('private-token', Path(source.askpass).read_text())
            self.assertIn('http.followRedirects=false', args[0])

    def test_self_hosted_gitlab_and_encoded_source_paths(self):
        url, platform = normalize_url('https://Git.Example.com:8443/a/b/c')
        self.assertEqual(url, 'https://git.example.com:8443/a/b/c.git')
        self.assertEqual(platform, 'gitlab')
        self.assertIn('a%20b.py#L2-4', source_url(url, platform, 'a' * 40, 'a b.py', 2, 4))


if __name__ == '__main__':
    unittest.main()
