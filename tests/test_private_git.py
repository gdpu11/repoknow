"""Exercise real Git HTTP authentication against a temporary private server."""
import base64
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from urllib.parse import urlsplit

from fastapi.testclient import TestClient

from app.git_source import GitSource
from app.main import create_app
from app.scanner import scan


class PrivateGitTests(unittest.TestCase):
    def test_authenticated_fetch_default_branch_and_blob_indexing(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            bare = root / 'remote/team/private.git'
            bare.parent.mkdir(parents=True)
            subprocess.run(['git', 'init', '--bare', '--initial-branch=main', str(bare)],
                           check=True, capture_output=True)
            env = os.environ | {'GIT_AUTHOR_NAME': 'Test Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.invalid',
                                'GIT_COMMITTER_NAME': 'Test Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.invalid'}

            def git(*args, data=None):
                return subprocess.run(['git', '--git-dir=' + str(bare), *args], input=data,
                                      capture_output=True, env=env, check=True).stdout.strip()

            readme = git('hash-object', '-w', '--stdin', data=b'# Private fixture\n\nAuthentication test.\n')
            source = git('hash-object', '-w', '--stdin', data=b'def hello():\n    return "private-evidence"\n')
            tree = git('mktree', data=b'100644 blob ' + readme + b'\tREADME.md\n100644 blob ' + source + b'\tapp.py\n')
            # A commit object in a disposable bare fixture, never a product commit.
            commit = git('commit-tree', tree.decode(), data=b'private HTTP fixture\n')
            git('update-ref', 'refs/heads/main', commit.decode())
            token = 'private-read-only-fixture-token'
            expected = 'Basic ' + base64.b64encode(('reader:' + token).encode()).decode()
            authenticated = []

            class Handler(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_GET(self):
                    self.serve_git()

                def do_POST(self):
                    self.serve_git()

                def serve_git(self):
                    if self.path.startswith('/api/v4/groups/'):
                        if self.headers.get('PRIVATE-TOKEN') != token:
                            self.send_response(401)
                            self.send_header('Content-Length', '0')
                            self.end_headers()
                            return
                        if '/projects?' in self.path:
                            document = [{'path_with_namespace': 'team/private', 'default_branch': 'main',
                                'http_url_to_repo': f'http://127.0.0.1:{self.server.server_port}/team/private.git'}]
                        else:
                            document = {'id': 7, 'full_path': 'team'}
                        raw = json.dumps(document).encode()
                        self.send_response(200)
                        self.send_header('Content-Type', 'application/json')
                        self.send_header('Content-Length', str(len(raw)))
                        self.end_headers()
                        self.wfile.write(raw)
                        return
                    if self.headers.get('Authorization') != expected:
                        self.send_response(401)
                        self.send_header('WWW-Authenticate', 'Basic realm="private-fixture"')
                        self.send_header('Content-Length', '0')
                        self.end_headers()
                        return
                    authenticated.append(self.path)
                    parsed = urlsplit(self.path)
                    request_env = env | {'GIT_PROJECT_ROOT': str(root / 'remote'), 'GIT_HTTP_EXPORT_ALL': '1',
                        'PATH_INFO': parsed.path, 'QUERY_STRING': parsed.query, 'REQUEST_METHOD': self.command,
                        'CONTENT_TYPE': self.headers.get('Content-Type', ''),
                        'CONTENT_LENGTH': self.headers.get('Content-Length', '0'), 'REMOTE_USER': 'reader',
                        'SERVER_PROTOCOL': 'HTTP/1.1', 'GIT_PROTOCOL': self.headers.get('Git-Protocol', '')}
                    body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
                    result = subprocess.run(['git', 'http-backend'], input=body, env=request_env,
                                            capture_output=True, check=True).stdout
                    header, payload = result.split(b'\r\n\r\n', 1)
                    lines = header.decode().split('\r\n')
                    status = next((int(line.split()[1]) for line in lines if line.startswith('Status:')), 200)
                    self.send_response(status)
                    for line in lines:
                        name, value = line.split(':', 1)
                        if name != 'Status':
                            self.send_header(name, value.strip())
                    self.send_header('Content-Length', str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)

            server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                data = root / 'data'
                data.mkdir()
                # A helper left by an earlier installation must not break authentication.
                previous_helper = data / 'git-askpass.sh'
                previous_helper.write_text('#!/bin/sh\nexit 71\n')
                previous_helper.chmod(0o700)
                git_source = GitSource(data, threading.Event())
                repo = {'id': 'private-fixture', 'url': f'http://127.0.0.1:{server.server_port}/team/private.git', 'branch': ''}
                directory, branch, actual = git_source.fetch(repo, {'username': 'reader', 'token': token})
                self.assertEqual(branch, 'main')
                self.assertEqual(actual, commit.decode())
                self.assertTrue(authenticated)
                files, symbols, _, _, statistics = scan(git_source, directory, actual, 'snapshot')
                self.assertEqual(statistics['files'], 2)
                self.assertEqual([s['name'] for s in symbols], ['hello'])
                self.assertNotIn(token, (directory / 'config').read_text())
                self.assertNotIn(token, (directory / 'FETCH_HEAD').read_text())
                with self.assertRaises(RuntimeError) as failure:
                    git_source.fetch(repo, {'username': 'reader', 'token': 'wrong-private-token'})
                self.assertNotIn('wrong-private-token', str(failure.exception))
                # Full management workflow: saved platform Token -> private
                # group discovery -> durable job -> authenticated Git indexing.
                app = create_app(root / 'application-data', start_worker=False)
                with TestClient(app) as client:
                    client.headers['X-Requested-With'] = 'RepoKnow'
                    account = json.loads((root / 'application-data/admin.json').read_text())
                    client.post('/api/auth/login', json={key: account[key] for key in ('username', 'password')})
                    connection = client.post('/api/connections', json={
                        'base_url': f'http://127.0.0.1:{server.server_port}', 'platform': 'gitlab',
                        'username': 'reader', 'token': token})
                    self.assertEqual(connection.status_code, 201, connection.text)
                    imported = client.post('/api/groups/import', json={
                        'url': f'http://127.0.0.1:{server.server_port}/team'})
                    self.assertEqual(imported.status_code, 200, imported.text)
                    self.assertEqual(imported.json()['created'], 1)
                    app.state.engine.tick()
                    indexed = client.get('/api/repositories').json()[0]
                    self.assertEqual(indexed['sync_state'], 'ready', indexed['error'])
                    self.assertEqual(indexed['commit_id'], commit.decode())
                    self.assertTrue(client.get('/api/search', params={'q': 'private-evidence'}).json()['results'])
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


if __name__ == '__main__':
    unittest.main()
