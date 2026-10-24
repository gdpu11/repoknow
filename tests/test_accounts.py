import hashlib
import json
import os
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from app.main import create_app
from app.store import Store
from test_core import FakeGit


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        with patch.dict(os.environ, {'REPOKNOW_ALLOW_REGISTRATION': '0'}):
            self.app = create_app(self.temp.name, start_worker=False)
        self.store = self.app.state.store
        self.client = TestClient(self.app)
        self.client.headers['X-Requested-With'] = 'RepoKnow'
        self.account = json.loads((self.store.root / 'admin.json').read_text())
        self.client.post('/api/auth/login', json={key: self.account[key] for key in ('username', 'password')})
        self.admin_id = self.client.get('/api/auth/me').json()['id']

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def member(self):
        credentials = {'username': 'fixture-member', 'password': 'Fixture-member-pass-123'}
        response = self.client.post('/api/admin/users', json=credentials | {'is_admin': True})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertFalse(response.json()['is_admin'])
        self.assertFalse(response.json()['disabled'])
        self.assertNotIn('password', response.json())
        return response.json()['id'], credentials

    def test_registration_is_closed_by_default(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ):
            os.environ.pop('REPOKNOW_ALLOW_REGISTRATION', None)
            with TestClient(create_app(directory, start_worker=False)) as anonymous:
                anonymous.headers['X-Requested-With'] = 'RepoKnow'
                self.assertEqual(anonymous.get('/api/auth/config').json(), {'registration_enabled': False})
                response = anonymous.post('/api/auth/register', json={
                    'username': 'uninvited', 'password': 'Fixture-password-123'})
                self.assertEqual(response.status_code, 403)
        self.assertTrue(self.client.get('/api/auth/me').json()['is_admin'])

    def test_public_registration_requires_opt_in_and_never_grants_admin(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'REPOKNOW_ALLOW_REGISTRATION': '1'}):
            with TestClient(create_app(directory, start_worker=False)) as client:
                client.headers['X-Requested-With'] = 'RepoKnow'
                self.assertTrue(client.get('/api/auth/config').json()['registration_enabled'])
                fields = {'username': 'public-member', 'password': 'Fixture-password-123', 'is_admin': True}
                self.assertEqual(client.post('/api/auth/register', json=fields).status_code, 201)
                self.assertEqual(client.post('/api/auth/login', json=fields).status_code, 200)
                self.assertFalse(client.get('/api/auth/me').json()['is_admin'])
                self.assertEqual(client.get('/api/admin/users').status_code, 403)

    def test_admin_creates_members_without_exposing_password_hashes(self):
        member_id, credentials = self.member()
        response = self.client.get('/api/admin/users')
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()), 2)
        self.assertNotIn(credentials['password'], response.text)
        with self.store.db() as con:
            encoded = con.execute('SELECT password FROM users WHERE id=?', (member_id,)).fetchone()[0]
        self.assertNotIn(encoded, response.text)
        self.assertNotEqual(encoded, credentials['password'])
        self.assertEqual(self.client.post('/api/admin/users', json=credentials).status_code, 409)

    def test_non_admin_and_read_only_keys_cannot_manage_accounts(self):
        member_id, credentials = self.member()
        with TestClient(self.app) as member:
            member.headers['X-Requested-With'] = 'RepoKnow'
            self.assertEqual(member.post('/api/auth/login', json=credentials).status_code, 200)
            self.assertEqual(member.get('/api/admin/users').status_code, 403)
            self.assertEqual(member.post('/api/admin/users', json={'username': 'intruder', 'password': 'Fixture-password-123'}).status_code, 403)
            self.assertEqual(member.put('/api/admin/users/' + member_id, json={'disabled': True}).status_code, 403)
        headers = {'Authorization': 'Bearer ' + self.account['read_only_api_key']}
        self.assertEqual(self.client.get('/api/admin/users', headers=headers).status_code, 403)
        self.assertEqual(self.client.post('/api/admin/users', json=credentials, headers=headers).status_code, 403)
        self.assertEqual(self.client.put('/api/admin/users/' + member_id, json={'disabled': True}, headers=headers).status_code, 403)

    def test_disabling_member_revokes_web_and_api_sessions_and_preserves_data(self):
        member_id, credentials = self.member()
        with TestClient(self.app) as member:
            member.headers['X-Requested-With'] = 'RepoKnow'
            member.post('/api/auth/login', json=credentials)
            repo = member.post('/api/repositories', json={'url': 'https://github.com/example/member-project'}).json()
            token = 'fixture-member-read-only-key'
            with self.store.db(write=True) as con:
                con.execute('INSERT INTO sessions VALUES (?,?,?,?)',
                            (hashlib.sha256(token.encode()).hexdigest(), member_id, 'api', 0))
            self.assertIsNotNone(self.store.authenticate(token))
            result = self.client.put('/api/admin/users/' + member_id, json={'disabled': True})
            self.assertEqual(result.status_code, 200)
            self.assertEqual(member.get('/api/auth/me').status_code, 401)
            self.assertIsNone(self.store.authenticate(token))
            self.assertEqual(member.post('/api/auth/login', json=credentials).status_code, 401)
            with self.store.db() as con:
                self.assertEqual(con.execute('SELECT owner_id FROM repositories WHERE id=?', (repo['id'],)).fetchone()[0], member_id)
            self.client.put('/api/admin/users/' + member_id, json={'disabled': False})
            self.assertIsNone(self.store.authenticate(token))
            self.assertEqual(member.post('/api/auth/login', json=credentials).status_code, 200)
            self.assertEqual(len(member.get('/api/repositories').json()), 1)

    def test_administrator_cannot_be_disabled_and_unknown_account_is_not_found(self):
        self.assertEqual(self.client.put('/api/admin/users/' + self.admin_id, json={'disabled': True}).status_code, 422)
        self.assertEqual(self.client.put('/api/admin/users/missing', json={'disabled': True}).status_code, 404)
        self.assertEqual(self.client.get('/api/auth/me').status_code, 200)

    def test_legacy_upgrade_preserves_credentials_sessions_indexes_and_promotes_only_bootstrap(self):
        member_id, _ = self.member()
        engine = self.app.state.engine
        engine.git = FakeGit(engine.stop_event)
        repo_id = self.client.post('/api/repositories', json={'url': 'https://github.com/example/legacy-project',
            'username': 'fixture-reader', 'token': 'fixture-git-access-token'}).json()['id']
        engine.tick()
        before = self.client.get('/api/repositories/' + repo_id).json()['current_snapshot']
        account_bytes = (self.store.root / 'admin.json').read_bytes()
        key_bytes = (self.store.root / 'credentials.key').read_bytes()
        with self.store.db(write=True) as con:
            con.execute('ALTER TABLE users DROP COLUMN is_admin')
            con.execute('ALTER TABLE users DROP COLUMN disabled')
        upgraded = Store(self.temp.name)
        self.assertEqual((self.store.root / 'admin.json').read_bytes(), account_bytes)
        self.assertEqual((self.store.root / 'credentials.key').read_bytes(), key_bytes)
        self.assertTrue(self.client.get('/api/auth/me').json()['is_admin'])
        with upgraded.db() as con:
            self.assertEqual(con.execute('SELECT is_admin FROM users WHERE id=?', (member_id,)).fetchone()[0], 0)
        self.assertEqual(self.client.get('/api/repositories/' + repo_id).json()['current_snapshot'], before)
        self.assertTrue(self.client.get('/api/search', params={'q': 'helper'}).json()['results'])
        with upgraded.db(write=True) as con:
            con.execute('UPDATE users SET is_admin=0 WHERE id=?', (self.admin_id,))
        Store(self.temp.name)
        with upgraded.db() as con:
            self.assertEqual(con.execute('SELECT is_admin FROM users WHERE id=?', (self.admin_id,)).fetchone()[0], 0)


if __name__ == '__main__':
    unittest.main()
