import io
import json
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from app.main import create_app
from app.providers import discover_group, discover_organization, get_json, normalize_organization
from test_core import FakeGit


def project(name, branch='main', host='https://github.com'):
    return {'full_name':name, 'owner':{'login':name.split('/')[0]},
            'clone_url':host + '/' + name + '.git', 'default_branch':branch}


class OrganizationDiscoveryTests(unittest.TestCase):
    def test_pagination_deduplication_and_default_branch_skips(self):
        calls = []

        def api(url, token, platform):
            self.assertEqual(platform, 'github')
            self.assertEqual(token, 'fixture-org-access-token')
            self.assertNotIn(token, url)
            calls.append(url)
            if '/repos?' not in url:
                return {'login':'Example-Org','type':'Organization'}, ''
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query['type'], ['all'])
            self.assertEqual(query['per_page'], ['100'])
            if query['page'] == ['1']:
                return [project(f'Example-Org/repo-{index}') for index in range(100)], ''
            return [project('Example-Org/repo-0'), project('Example-Org/last'), project('Example-Org/empty', None)], ''

        with patch('app.providers.get_json', side_effect=api):
            result = discover_organization('https://github.com/example-org', {'token':'fixture-org-access-token'})
        self.assertEqual(len(calls), 3)
        self.assertTrue(all(url.startswith('https://api.github.com/orgs/') for url in calls))
        self.assertEqual(len(result['projects']), 101)
        self.assertEqual(len(result['skipped']), 1)
        self.assertEqual(result['url'], 'https://github.com/Example-Org')

    def test_enterprise_api_prefix_and_organization_identity(self):
        calls = []

        def api(url, token, platform):
            calls.append(url)
            return ({'login':'engineering','type':'Organization'}, '') if '/repos?' not in url else (
                [project('engineering/api', host='https://github.example.com')], '')

        with patch('app.providers.get_json', side_effect=api):
            result = discover_organization('https://github.example.com/engineering')
        self.assertTrue(all(url.startswith('https://github.example.com/api/v3/orgs/engineering') for url in calls))
        self.assertEqual(result['projects'][0]['url'], 'https://github.example.com/engineering/api.git')
        for metadata in ({'login':'engineering','type':'User'}, {'login':'foreign','type':'Organization'}):
            with self.subTest(metadata=metadata), patch('app.providers.get_json', return_value=(metadata, '')):
                with self.assertRaises(ValueError):
                    discover_organization('https://github.com/engineering')

    def test_foreign_owner_origin_and_mismatched_clone_path_are_rejected(self):
        malformed = [project('elsewhere/api'), project('engineering/api', host='https://attacker.example')]
        wrong_path = project('engineering/api')
        wrong_path['clone_url'] = 'https://github.com/engineering/other.git'
        malformed.append(wrong_path)
        wrong_owner = project('engineering/api')
        wrong_owner['owner']['login'] = 'elsewhere'
        malformed.append(wrong_owner)
        for repository in malformed:
            with self.subTest(repository=repository), patch('app.providers.get_json', side_effect=[
                    ({'login':'engineering','type':'Organization'}, ''), ([repository], '')]):
                with self.assertRaises(ValueError):
                    discover_organization('https://github.com/engineering')
        with patch('app.providers.get_json', side_effect=[({'id':1,'full_path':'engineering'}, ''),
                ([{'path_with_namespace':'engineering/api','http_url_to_repo':'https://gitlab.example.com/other/api.git',
                   'default_branch':'main'}], '')]):
            with self.assertRaisesRegex(ValueError, '路径不匹配'):
                discover_group('https://gitlab.example.com/engineering')

    def test_repository_cap_and_discovery_deadline(self):
        with patch('app.providers.get_json', side_effect=[({'login':'engineering','type':'Organization'}, ''),
                ([project(f'engineering/repo-{index}') for index in range(5001)], '')]):
            with self.assertRaisesRegex(ValueError, '5000'):
                discover_organization('https://github.com/engineering')
        with patch('app.providers.get_json', return_value=({'login':'engineering','type':'Organization'}, '')), \
                patch('app.providers.time.monotonic', side_effect=[0, 121]):
            with self.assertRaisesRegex(ValueError, '超时'):
                discover_organization('https://github.com/engineering')

    def test_invalid_organization_url_does_not_reach_network(self):
        for url in ('https://github.com/team/repository', 'https://github.com/team?token=value',
                    'https://' + 'user:password' + '@github.com/team', 'https://github.com/team%2fother',
                    'https://github.com/..', 'https://github.com/%00', 'file:///team'):
            with self.subTest(url=url), patch('app.providers.get_json') as request:
                with self.assertRaises(ValueError):
                    discover_organization(url)
                request.assert_not_called()
        self.assertEqual(normalize_organization('github.com/example-org/'), ('https://github.com','example-org'))

    def test_github_headers_and_errors_never_echo_private_token(self):
        token = 'fixture-private-github-access-token'
        response = io.BytesIO(b'{}')
        response.headers = {}
        with patch('app.providers.build_opener') as opener:
            opener.return_value.open.return_value = response
            get_json('https://api.github.com/orgs/engineering', token, platform='github')
            request = opener.return_value.open.call_args.args[0]
            headers = {key.lower():value for key,value in request.header_items()}
            self.assertEqual(headers['authorization'], 'Bearer ' + token)
            self.assertEqual(headers['accept'], 'application/vnd.github+json')
            self.assertNotIn('private-token', headers)
        for status in (301,401,403,404,429,500):
            error = HTTPError('https://api.github.com/orgs/engineering', status, token, {}, io.BytesIO(token.encode()))
            with self.subTest(status=status), patch('app.providers.build_opener') as opener:
                opener.return_value.open.side_effect = error
                with self.assertRaises(ValueError) as failure:
                    get_json('https://api.github.com/orgs/engineering', token, platform='github')
                self.assertNotIn(token, str(failure.exception))


class OrganizationImportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = create_app(self.temp.name, start_worker=False)
        self.client = TestClient(self.app)
        self.client.headers['X-Requested-With'] = 'RepoKnow'
        self.account = json.loads((self.app.state.store.root / 'admin.json').read_text())
        self.client.post('/api/auth/login', json={key:self.account[key] for key in ('username','password')})
        self.git = FakeGit(self.app.state.engine.stop_event)
        self.app.state.engine.git = self.git
        self.token = 'fixture-organization-token'
        self.connection_id = self.client.post('/api/connections', json={'base_url':'https://github.com',
            'platform':'github','username':'fixture-reader','token':self.token}).json()['id']

    def tearDown(self):
        self.client.close()
        self.temp.cleanup()

    def api(self, url, token, platform):
        self.assertEqual((token,platform), (self.token,'github'))
        if '/repos?' not in url:
            return {'login':'engineering','type':'Organization'}, ''
        return [project('engineering/' + 'r' * 100), project('engineering/api'), project('engineering/empty', None)], ''

    def test_saved_token_preview_import_idempotency_and_search(self):
        body = {'url':'https://github.com/engineering'}
        with patch('app.providers.get_json', side_effect=self.api):
            preview = self.client.post('/api/organizations/discover', json=body)
            self.assertEqual(preview.status_code, 200, preview.text)
            self.assertNotIn(self.token, preview.text)
            self.assertEqual(self.client.get('/api/repositories').json(), [])
            imported = self.client.post('/api/organizations/import', json=body)
            self.assertEqual(imported.status_code, 200, imported.text)
            self.assertEqual(imported.json()['created'], 2)
            self.assertEqual(len(imported.json()['skipped']), 1)
            repeated = self.client.post('/api/organizations/import', json=body).json()
            self.assertEqual((repeated['created'],repeated['existing']), (0,2))
        with patch.object(self.git, 'fetch', wraps=self.git.fetch) as fetch:
            self.app.state.engine.tick()
            self.app.state.engine.tick()
        self.assertTrue(all(call.args[1]['token'] == self.token for call in fetch.call_args_list))
        repositories = self.client.get('/api/repositories').json()
        self.assertTrue(all(repo['platform']=='github' and repo['connection_id']==self.connection_id and repo['sync_state']=='ready' for repo in repositories))
        with self.app.state.store.db() as con:
            self.assertEqual(con.execute('SELECT count(*) FROM jobs').fetchone()[0], 2)
        self.assertTrue(self.client.get('/api/search', params={'q':'helper'}).json()['results'])

    def test_late_page_failure_and_invalid_branch_leave_no_partial_import(self):
        for pages in ([({'login':'engineering','type':'Organization'}, ''),
                       ([project(f'engineering/r-{index}') for index in range(100)], ''), ValueError('fixture page failure')],
                      [({'login':'engineering','type':'Organization'}, ''),
                       ([project('engineering/valid'),project('engineering/invalid','-invalid')], '')]):
            with patch('app.providers.get_json', side_effect=pages):
                response = self.client.post('/api/organizations/import', json={'url':'https://github.com/engineering'})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(self.client.get('/api/repositories').json(), [])
            with self.app.state.store.db() as con:
                self.assertEqual(con.execute('SELECT count(*) FROM jobs').fetchone()[0], 0)

    def test_credentials_cannot_cross_owners_or_platforms_and_read_only_key_is_denied(self):
        self.client.post('/api/admin/users', json={'username':'other-member','password':'Fixture-password-123'})
        with TestClient(self.app) as other:
            other.headers['X-Requested-With'] = 'RepoKnow'
            other.post('/api/auth/login', json={'username':'other-member','password':'Fixture-password-123'})
            with patch('app.providers.get_json') as network:
                response = other.post('/api/organizations/discover', json={
                    'url':'https://github.com/engineering','connection_id':self.connection_id})
                self.assertEqual(response.status_code, 422)
                network.assert_not_called()
        with self.app.state.store.db(write=True) as con:
            con.execute("UPDATE connections SET platform='gitlab' WHERE id=?", (self.connection_id,))
        with patch('app.providers.get_json') as network:
            self.assertEqual(self.client.post('/api/organizations/import', json={'url':'https://github.com/engineering'}).status_code, 422)
            network.assert_not_called()
        headers = {'Authorization':'Bearer ' + self.account['read_only_api_key']}
        for action in ('discover','import'):
            self.assertEqual(self.client.post('/api/organizations/' + action,
                json={'url':'https://github.com/engineering'},headers=headers).status_code, 403)


if __name__ == '__main__':
    unittest.main()
