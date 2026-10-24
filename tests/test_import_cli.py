import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


ROOT = Path(__file__).resolve().parents[1]


class ImportCommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.data = Path(self.temporary.name)
        (self.data / 'admin.json').write_text(json.dumps({
            'username': 'fixture-admin', 'password': 'fixture-password-for-cli'}))
        self.requests = []
        self.imported = False
        self.failed = False
        test = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.respond()

            def do_POST(self):
                self.respond()

            def respond(self):
                raw = self.rfile.read(int(self.headers.get('Content-Length', 0)))
                body = json.loads(raw) if raw else None
                test.requests.append((self.path, body))
                payload = {}
                if self.path == '/api/auth/me':
                    payload = {'id': 'fixture-owner'}
                elif self.path.endswith('/import'):
                    test.imported = True
                    payload = {'repositories': [{'id': 'fixture-repository'}]}
                elif self.path == '/api/repositories':
                    payload = ([{'id': 'fixture-repository', 'name': 'example/project',
                                 'sync_state': 'failed' if test.failed else 'ready',
                                 'error': 'fixture failure' if test.failed else '',
                                 'commit_id': 'a' * 40}] if test.imported else [])
                encoded = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, *_args):
                pass

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.close_server)
        self.base = f'http://127.0.0.1:{self.server.server_port}'

    def close_server(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def run_command(self, *args, base=None):
        environment = dict(os.environ, REPOKNOW_URL=base or self.base,
                           REPOKNOW_DATA_DIR=str(self.data))
        return subprocess.run([sys.executable, str(ROOT / 'scripts/import_source.py'), *args],
                              env=environment, capture_output=True, text=True, timeout=10)

    def test_namespace_selection_and_wait_use_correct_management_api(self):
        cases = [
            (['https://github.com/example-org', '--wait'], 'organizations', False),
            (['https://github.example.com/engineering', '--kind', 'organization', '--wait'],
             'organizations', False),
            (['https://gitlab.example.com/engineering', '--no-subgroups', '--wait'], 'groups', True),
        ]
        for arguments, endpoint, subgroup_field in cases:
            with self.subTest(arguments=arguments):
                self.requests.clear()
                self.imported = False
                result = self.run_command(*arguments)
                self.assertEqual(result.returncode, 0, result.stderr)
                body = next(body for path, body in self.requests if path == f'/api/{endpoint}/import')
                self.assertEqual(body['url'], arguments[0])
                self.assertEqual('include_subgroups' in body, subgroup_field)
                if subgroup_field:
                    self.assertFalse(body['include_subgroups'])
                self.assertIn('index_results', result.stdout)
                self.assertIn('ready', result.stdout)
                self.assertNotIn('fixture-password-for-cli', result.stdout + result.stderr)

    def test_wait_does_not_report_failed_index_as_success(self):
        self.failed = True
        result = self.run_command('https://github.com/example-org', '--wait')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('fixture failure', result.stdout)
        self.assertIn('部分仓库索引失败', result.stderr)

    def test_remote_management_url_is_rejected_before_reading_credentials(self):
        (self.data / 'admin.json').unlink()
        result = self.run_command('https://github.com/example-org', base='https://example.com')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('未发送管理员凭证', result.stderr)
        self.assertEqual(self.requests, [])


if __name__ == '__main__':
    unittest.main()
