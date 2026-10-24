import io
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from app.main import create_app
from app.providers import discover_group, get_json
from app.store import Store
from test_core import FakeGit


def project(name, branch='main', host='https://gitlab.example.com'):
    return {'path_with_namespace': name, 'http_url_to_repo': host + '/' + name + '.git',
            'default_branch': branch, 'empty_repo': not bool(branch)}


class GroupImportTests(unittest.TestCase):
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
        self.account = json.loads((self.store.root / 'admin.json').read_text())
        self.client.post('/api/auth/login', json={key: self.account[key] for key in ('username', 'password')})

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.temp.cleanup()

    def connection(self):
        response = self.client.post('/api/connections', json={'name': 'Work GitLab',
            'base_url': 'https://gitlab.example.com', 'platform': 'gitlab',
            'username': 'git-reader', 'token': 'private-test-token-for-groups'})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertNotIn('private-test-token-for-groups', response.text)
        return response.json()['id']

    def test_platform_credentials_encrypted_reused_and_rotated(self):
        connection_id = self.connection()
        self.assertNotIn('private-test-token-for-groups', self.client.get('/api/connections').text)
        with self.store.db() as con:
            blob = con.execute('SELECT credential FROM connections WHERE id=?', (connection_id,)).fetchone()[0]
        self.assertNotIn(b'private-test-token-for-groups', blob)
        added = self.client.post('/api/repositories', json={'url': 'https://gitlab.example.com/team/one.git'}).json()
        repo_id = added['id']
        with patch.object(self.git, 'fetch', wraps=self.git.fetch) as fetch:
            self.engine.tick()
            self.assertEqual(fetch.call_args.args[1], {'username': 'git-reader', 'token': 'private-test-token-for-groups'})
        self.assertEqual(self.client.get('/api/repositories/' + repo_id).json()['connection_id'], connection_id)
        updated = self.client.put('/api/connections/' + connection_id, json={'token': 'rotated-private-token'})
        self.assertEqual(updated.status_code, 200)
        self.client.put('/api/connections/' + connection_id, json={'name': 'Renamed', 'token': ''})
        self.store.queue(repo_id)
        with patch.object(self.git, 'fetch', wraps=self.git.fetch) as fetch:
            self.engine.tick()
            self.assertEqual(fetch.call_args.args[1], {'username': 'git-reader', 'token': 'rotated-private-token'})
        self.assertEqual(self.client.get('/api/repositories/' + repo_id).json()['sync_state'], 'ready')

    def test_connections_cannot_cross_users_or_origins(self):
        connection_id = self.connection()
        rejected = self.client.post('/api/repositories', json={
            'url': 'https://attacker.example/team/repo.git', 'connection_id': connection_id})
        self.assertEqual(rejected.status_code, 422)
        with TestClient(self.app) as other:
            other.headers['X-Requested-With'] = 'RepoKnow'
            other.post('/api/auth/register', json={'username': 'outsider', 'password': 'Outsider-password-123'})
            other.post('/api/auth/login', json={'username': 'outsider', 'password': 'Outsider-password-123'})
            self.assertEqual(other.get('/api/connections').json(), [])
            self.assertEqual(other.put('/api/connections/' + connection_id, json={'token': 'stolen'}).status_code, 404)
            with patch('app.main.discover_group') as discover:
                response = other.post('/api/groups/import', json={'url': 'https://gitlab.example.com/team',
                                                                  'connection_id': connection_id})
                self.assertEqual(response.status_code, 422)
                discover.assert_not_called()
        headers = {'Authorization': 'Bearer ' + self.account['read_only_api_key']}
        self.assertEqual(self.client.post('/api/groups/import', json={'url': 'https://gitlab.example.com/team'},
                                         headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/api/connections', json={'base_url': 'https://example.com'},
                                         headers=headers).status_code, 403)

    def test_explicit_repository_credentials_remain_independent(self):
        self.connection()
        response = self.client.post('/api/repositories', json={'url': 'https://gitlab.example.com/team/one',
                                                              'username': 'repo-user', 'token': 'repo-token'})
        repo_id = response.json()['id']
        self.assertIsNone(self.client.get('/api/repositories/' + repo_id).json()['connection_id'])
        with patch.object(self.git, 'fetch', wraps=self.git.fetch) as fetch:
            self.engine.tick()
            self.assertEqual(fetch.call_args.args[1]['token'], 'repo-token')
        linked = self.client.post('/api/repositories', json={'url': 'https://gitlab.example.com/team/two'}).json()['id']
        self.client.put(f'/api/repositories/{linked}/credentials', json={'username': 'independent', 'token': 'override'})
        self.assertIsNone(self.client.get('/api/repositories/' + linked).json()['connection_id'])

    def test_recursive_group_import_is_paginated_idempotent_and_independent(self):
        self.connection()
        original = self.client.post('/api/repositories', json={'url': 'https://gitlab.example.com/team/one', 'branch': 'main'}).json()['id']
        calls = []

        def api(url, token):
            self.assertEqual(token, 'private-test-token-for-groups')
            calls.append(url)
            if '/projects?' not in url:
                return {'id': 7, 'full_path': 'team'}, ''
            if parse_qs(urlsplit(url).query)['page'] == ['1']:
                return [project('team/one'), project('team/subgroup/two')], '2'
            return [project('team/subgroup/two'), project('team/empty', None)], ''

        with patch('app.providers.get_json', side_effect=api):
            preview = self.client.post('/api/groups/discover', json={'url': 'gitlab.example.com/team'}).json()
            self.assertEqual(len(preview['projects']), 2)
            self.assertEqual(len(self.client.get('/api/repositories').json()), 1)
            first = self.client.post('/api/groups/import', json={'url': 'https://gitlab.example.com/team'}).json()
            self.assertEqual((first['created'], first['existing'], len(first['skipped'])), (1, 1, 1))
            second = self.client.post('/api/groups/import', json={'url': 'https://gitlab.example.com/team',
                                                                'visibility': 'shared', 'include_subgroups': False}).json()
            self.assertEqual((second['created'], second['existing']), (0, 2))
        self.assertTrue(any('include_subgroups=true' in url for url in calls))
        self.assertTrue(any('include_subgroups=false' in url for url in calls))
        self.assertTrue(all('with_shared=false' in url for url in calls if '/projects?' in url))
        self.assertEqual(self.client.get('/api/repositories/' + original).json()['visibility'], 'private')
        self.assertTrue(self.engine.tick())
        # Failure of one repository must not prevent the next repository's indexing.
        with patch.object(self.git, 'fetch', side_effect=RuntimeError('fixture unavailable')):
            self.assertTrue(self.engine.tick())
        states = {row['sync_state'] for row in self.client.get('/api/repositories').json()}
        self.assertEqual(states, {'ready', 'failed'})
        self.assertFalse(self.engine.tick())

    def test_later_page_failure_leaves_no_partial_import(self):
        with patch('app.providers.get_json', side_effect=[
            ({'id': 7, 'full_path': 'team'}, ''), ([project('team/one')], '2'),
            ValueError('API rate limited')]):
            response = self.client.post('/api/groups/import', json={'url': 'https://gitlab.example.com/team'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get('/api/repositories').json(), [])

    def test_foreign_clone_url_is_rejected_before_credentials_or_jobs(self):
        self.connection()
        with patch('app.providers.get_json', side_effect=[
            ({'id': 7, 'full_path': 'team'}, ''), ([project('team/one', host='https://attacker.example')], '')]):
            response = self.client.post('/api/groups/import', json={'url': 'https://gitlab.example.com/team'})
        self.assertEqual(response.status_code, 422)
        self.assertNotIn('private-test-token-for-groups', response.text)
        self.assertEqual(self.client.get('/api/repositories').json(), [])

    def test_existing_database_upgrade_preserves_index_and_login(self):
        repo_id = self.client.post('/api/repositories', json={'url': 'https://gitlab.example.com/team/one'}).json()['id']
        self.engine.tick()
        before = self.client.get('/api/repositories/' + repo_id).json()['current_snapshot']
        with self.store.db(write=True) as con:
            con.execute('ALTER TABLE repositories DROP COLUMN connection_id')
        upgraded = Store(self.temp.name)
        self.assertTrue(upgraded.login(self.account['username'], self.account['password']))
        after = self.client.get('/api/repositories/' + repo_id).json()
        self.assertEqual(after['current_snapshot'], before)
        self.assertIsNone(after['connection_id'])
        self.assertTrue(self.client.get('/api/search', params={'q': 'helper'}).json()['results'])


class DiscoveryTests(unittest.TestCase):
    def test_nested_group_encoding_and_pagination_without_next_header(self):
        calls = []

        def api(url, token):
            calls.append(url)
            if '/projects?' not in url:
                return {'id': 77, 'full_path': 'team/sub'}, ''
            page = int(parse_qs(urlsplit(url).query)['page'][0])
            count = 100 if page == 1 else 1
            return [project(f'team/sub/repo{page}-{i}') for i in range(count)], ''

        with patch('app.providers.get_json', side_effect=api):
            result = discover_group('https://gitlab.example.com/team/sub')
        self.assertEqual(len(result['projects']), 101)
        self.assertIn('/groups/team%2Fsub', calls[0])
        self.assertEqual(len(calls), 3)

    def test_api_failures_do_not_echo_token_or_server_body(self):
        secret = 'private-token-never-echo'
        failure = HTTPError('https://gitlab.example.com/api/v4/groups/team', 403, 'forbidden', {},
                            io.BytesIO(secret.encode()))
        with patch('app.providers.build_opener') as opener:
            opener.return_value.open.side_effect = failure
            with self.assertRaisesRegex(ValueError, 'read_api') as error:
                get_json('https://gitlab.example.com/api/v4/groups/team', secret)
        self.assertNotIn(secret, str(error.exception))


if __name__ == '__main__':
    unittest.main()
