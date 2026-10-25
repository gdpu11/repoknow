import hashlib
import json
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.store import Store
from test_core import FakeGit


class TeamAccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.app = create_app(self.temp.name, start_worker=False)
        self.store = self.app.state.store
        self.engine = self.app.state.engine
        self.engine.git = FakeGit(self.engine.stop_event)
        self.admin = self.client()
        self.account = json.loads((self.store.root / 'admin.json').read_text())
        self.admin.post('/api/auth/login', json={k: self.account[k] for k in ('username', 'password')})
        credentials = {'username': 'team-member', 'password': 'Fixture-member-password'}
        self.member_id = self.admin.post('/api/admin/users', json=credentials).json()['id']
        self.member = self.client()
        self.member.post('/api/auth/login', json=credentials)

    def client(self):
        client = TestClient(self.app)
        client.headers['X-Requested-With'] = 'RepoKnow'
        self.addCleanup(client.close)
        return client

    def key(self, client=None, name='Work laptop'):
        response = (client or self.member).post('/api/auth/keys', json={'name': name})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers['cache-control'], 'no-store')
        return response.json()

    def indexed(self, client, name, visibility='private'):
        response = client.post('/api/repositories', json={
            'url': 'https://gitlab.example.com/team/' + name, 'visibility': visibility})
        self.assertEqual(response.status_code, 201)
        self.assertTrue(self.engine.tick())
        return client.get('/api/repositories/' + response.json()['id']).json()

    def mcp(self, key, name, arguments=None):
        response = self.member.post('/mcp', headers={'Authorization': 'Bearer ' + key}, json={
            'jsonrpc': '2.0', 'id': 1, 'method': 'tools/call',
            'params': {'name': name, 'arguments': arguments or {}}})
        self.assertEqual(response.status_code, 200)
        return response.json()['result']

    def data(self, key, name, arguments=None):
        result = self.mcp(key, name, arguments)
        self.assertNotIn('isError', result)
        return json.loads(result['content'][0]['text'])

    def test_member_key_searches_visible_repositories_without_management_access(self):
        hidden = self.indexed(self.admin, 'private')
        shared = self.indexed(self.admin, 'shared', 'shared')
        own = self.indexed(self.member, 'own')
        key = self.key()['key']
        visible = {shared['id'], own['id']}
        self.assertEqual({r['id'] for r in self.data(key, 'knowledge_catalog')}, visible)
        hits = self.data(key, 'knowledge_search', {'q': 'helper'})['results']
        self.assertEqual({h['repository_id'] for h in hits}, visible)
        for hit in hits:
            source = self.data(key, 'knowledge_get_source', {
                'repository_id': hit['repository_id'], 'commit': hit['commit_id'],
                'path': hit['path'], 'start_line': hit['start_line'], 'end_line': hit['end_line']})
            self.assertIn('helper', source['text'])
        self.assertTrue(self.mcp(key, 'knowledge_get_symbols', {'repository_id': hidden['id']})['isError'])
        headers = {'Authorization': 'Bearer ' + key}
        self.assertEqual(self.member.post('/api/repositories', headers=headers, json={
            'url': 'https://github.com/example/denied'}).status_code, 403)
        self.assertEqual(self.member.post('/api/repositories/' + own['id'] + '/sync', headers=headers).status_code, 403)
        self.assertEqual(self.member.put('/api/repositories/' + own['id'], headers=headers, json={
            'name': 'denied', 'visibility': 'shared'}).status_code, 403)

    def test_keys_are_hash_only_owned_and_individually_revocable(self):
        first, second = self.key(), self.key(name='Second client')
        listed = self.member.get('/api/auth/keys').json()
        self.assertEqual({k['id'] for k in listed}, {first['id'], second['id']})
        self.assertTrue(all(set(k) == {'id', 'name', 'created_at'} for k in listed))
        self.assertNotIn(first['key'], json.dumps(listed))
        with self.store.db() as con:
            stored = dict(con.execute('''SELECT k.*,s.* FROM api_keys k
                JOIN sessions s ON s.hash=k.session_hash WHERE k.id=?''', (first['id'],)).fetchone())
        self.assertEqual(stored['hash'], hashlib.sha256(first['key'].encode()).hexdigest())
        self.assertNotIn(first['key'], str(stored))
        self.assertEqual(self.admin.delete('/api/auth/keys/' + first['id']).status_code, 404)
        self.assertNotIn(first['id'], self.admin.get('/api/auth/keys').text)
        self.assertEqual(self.member.delete('/api/auth/keys/' + first['id']).status_code, 200)
        self.assertIsNone(self.store.authenticate(first['key']))
        self.assertIsNotNone(self.store.authenticate(second['key']))
        self.assertEqual(self.member.get('/api/auth/me').status_code, 200)
        self.assertEqual(self.member.delete('/api/auth/keys/' + first['id']).status_code, 404)
        with self.store.db() as con:
            self.assertFalse(con.execute('SELECT 1 FROM api_keys WHERE id=?', (first['id'],)).fetchone())
        Store(self.temp.name)
        self.assertIsNone(self.store.authenticate(first['key']))

    def test_keys_cannot_list_create_or_revoke_keys_even_with_a_valid_cookie(self):
        key = self.key()
        headers = {'Authorization': 'Bearer ' + key['key']}
        self.assertEqual(self.member.get('/api/auth/keys', headers=headers).status_code, 403)
        self.assertEqual(self.member.post('/api/auth/keys', headers=headers, json={'name': 'denied'}).status_code, 403)
        self.assertEqual(self.member.delete('/api/auth/keys/' + key['id'], headers=headers).status_code, 403)
        anonymous = self.client()
        self.assertEqual(anonymous.get('/api/auth/keys').status_code, 401)
        self.assertEqual(anonymous.post('/api/auth/keys', json={'name': 'denied'}).status_code, 401)

    def test_disabling_member_removes_key_metadata_and_cannot_race_key_creation(self):
        key = self.key()
        authenticate = self.store.authenticate

        def disable_after_authentication(token):
            principal = authenticate(token)
            if principal and principal['id'] == self.member_id:
                self.admin.put('/api/admin/users/' + self.member_id, json={'disabled': True})
            return principal

        with patch.object(self.store, 'authenticate', side_effect=disable_after_authentication):
            response = self.member.post('/api/auth/keys', json={'name': 'racing client'})
        self.assertEqual(response.status_code, 401)
        self.assertIsNone(self.store.authenticate(key['key']))
        with self.store.db() as con:
            self.assertFalse(con.execute('SELECT 1 FROM sessions WHERE user_id=?', (self.member_id,)).fetchone())
            self.assertFalse(con.execute('SELECT 1 FROM api_keys WHERE id=?', (key['id'],)).fetchone())
        self.admin.put('/api/admin/users/' + self.member_id, json={'disabled': False})
        Store(self.temp.name)
        self.assertIsNone(self.store.authenticate(key['key']))

    def test_legacy_keys_migrate_without_rotation_and_revocation_survives_restart(self):
        repo = self.indexed(self.admin, 'existing')
        account_bytes = (self.store.root / 'admin.json').read_bytes()
        encryption_key = (self.store.root / 'credentials.key').read_bytes()
        with self.store.db(write=True) as con:
            sessions = [tuple(r) for r in con.execute('SELECT * FROM sessions ORDER BY hash')]
            con.execute('DROP TABLE api_keys')
        Store(self.temp.name)
        with self.store.db() as con:
            self.assertEqual([tuple(r) for r in con.execute('SELECT * FROM sessions ORDER BY hash')], sessions)
        keys = self.admin.get('/api/auth/keys').json()
        self.assertEqual(len(keys), 1)
        Store(self.temp.name)
        self.assertEqual(self.admin.get('/api/auth/keys').json(), keys)
        self.assertIsNotNone(self.store.authenticate(self.account['read_only_api_key']))
        self.assertEqual(self.admin.get('/api/repositories/' + repo['id']).json()['current_snapshot'], repo['current_snapshot'])
        self.assertEqual(self.admin.delete('/api/auth/keys/' + keys[0]['id']).status_code, 200)
        Store(self.temp.name)
        self.assertIsNone(self.store.authenticate(self.account['read_only_api_key']))
        self.assertEqual(self.admin.get('/api/auth/keys').json(), [])
        self.assertEqual((self.store.root / 'admin.json').read_bytes(), account_bytes)
        self.assertEqual((self.store.root / 'credentials.key').read_bytes(), encryption_key)

    def test_revoked_web_session_cannot_mint_a_key_after_request_authentication(self):
        authenticate = self.store.authenticate

        def revoke_after_authentication(token):
            principal = authenticate(token)
            if principal and principal['id'] == self.member_id:
                with self.store.db(write=True) as con:
                    con.execute('DELETE FROM sessions WHERE hash=?', (principal['session_hash'],))
            return principal

        with patch.object(self.store, 'authenticate', side_effect=revoke_after_authentication):
            response = self.member.post('/api/auth/keys', json={'name': 'racing client'})
        self.assertEqual(response.status_code, 401)
        with self.store.db() as con:
            self.assertFalse(con.execute('SELECT 1 FROM sessions WHERE user_id=?', (self.member_id,)).fetchone())

    def test_key_names_and_limit_are_validated_without_partial_sessions(self):
        for name in ('', '   ', 'x' * 101):
            self.assertEqual(self.member.post('/api/auth/keys', json={'name': name}).status_code, 422)
        self.assertEqual(self.member.get('/api/auth/keys').json(), [])
        first = self.key(name='  Laptop  ')
        self.assertEqual(first['name'], 'Laptop')
        for i in range(19):
            self.key(name='Client ' + str(i))
        self.assertEqual(self.member.post('/api/auth/keys', json={'name': 'overflow'}).status_code, 409)
        self.assertEqual(len(self.member.get('/api/auth/keys').json()), 20)
        self.member.delete('/api/auth/keys/' + first['id'])
        self.key(name='replacement')
        with self.store.db() as con:
            self.assertEqual(con.execute("SELECT count(*) FROM sessions WHERE user_id=? AND kind='api'", (self.member_id,)).fetchone()[0], 20)

    def test_visibility_changes_apply_to_web_and_every_mcp_tool_without_reindexing(self):
        repo = self.indexed(self.admin, 'team-source')
        key = self.key()['key']
        path = '/api/repositories/' + repo['id']
        self.assertEqual(self.member.get(path).status_code, 404)
        with self.store.db() as con:
            before = dict(con.execute('SELECT * FROM repositories WHERE id=?', (repo['id'],)).fetchone())
            jobs = con.execute('SELECT count(*) FROM jobs').fetchone()[0]
        response = self.admin.put(path, json={'name': '  团队代码库  ', 'visibility': 'shared'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.member.get(path).json()['name'], '团队代码库')
        self.assertTrue(self.member.get('/api/search', params={'q': 'helper'}).json()['results'])
        symbols = self.data(key, 'knowledge_get_symbols', {'repository_id': repo['id']})['symbols']
        args = {
            'knowledge_get_symbols': {'repository_id': repo['id']},
            'knowledge_get_source': {'repository_id': repo['id'], 'commit': repo['commit_id'], 'path': 'main.py'},
            'knowledge_trace_dependency': {'repository_id': repo['id'], 'symbol_id': symbols[0]['id']},
        }
        for name, arguments in args.items():
            self.data(key, name, arguments)
        self.admin.put(path, json={'name': '团队代码库', 'visibility': 'private'})
        self.assertEqual(self.member.get(path).status_code, 404)
        self.assertEqual(self.member.get(path + '/file', params={'path': 'main.py'}).status_code, 404)
        self.assertEqual(self.member.get('/api/search', params={'q': 'helper'}).json()['results'], [])
        self.assertEqual(self.data(key, 'knowledge_catalog'), [])
        self.assertEqual(self.data(key, 'knowledge_search', {'q': 'helper'})['results'], [])
        for name, arguments in args.items():
            self.assertTrue(self.mcp(key, name, arguments)['isError'])
        with self.store.db() as con:
            after = dict(con.execute('SELECT * FROM repositories WHERE id=?', (repo['id'],)).fetchone())
            self.assertEqual(con.execute('SELECT count(*) FROM jobs').fetchone()[0], jobs)
        self.assertEqual(after, before | {'name': '团队代码库'})

    def test_only_owner_can_change_settings_and_invalid_changes_leave_index_intact(self):
        repo = self.indexed(self.member, 'member-owned', 'shared')
        path = '/api/repositories/' + repo['id']
        fields = {'name': 'Other name', 'visibility': 'private'}
        self.assertEqual(self.admin.put(path, json=fields).status_code, 404)
        for invalid in ({'name': '   '}, {'name': ''}, {'name': 'x' * 1001}, {'visibility': 'public'}):
            self.assertEqual(self.member.put(path, json=fields | invalid).status_code, 422)
        self.assertEqual(self.member.get(path).json(), repo)
        self.assertEqual(self.member.put('/api/repositories/missing', json=fields).status_code, 404)


if __name__ == '__main__':
    unittest.main()
