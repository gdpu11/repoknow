"""Import two public repositories and verify the real end-to-end workflow."""
import http.cookiejar
import json
import os
from pathlib import Path
import time
import urllib.error
import urllib.parse
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get('REPOKNOW_DATA_DIR', ROOT / '.data'))
BASE = os.environ.get('REPOKNOW_URL', 'http://127.0.0.1:8090')
EXAMPLES = [
    ('GitLab · Django 示例', 'https://gitlab.com/gitlab-examples/python-getting-started.git'),
    ('GitHub · Chi 路由框架', 'https://github.com/go-chi/chi.git'),
]


def main():
    credentials = json.loads((DATA / 'admin.json').read_text())
    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, body=None):
        req = urllib.request.Request(BASE + path,
            data=None if body is None else json.dumps(body).encode(),
            headers={'Content-Type': 'application/json', 'X-Requested-With': 'RepoKnow'})
        try:
            with client.open(req, timeout=30) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            raise RuntimeError(str(error.code) + ': ' + error.read().decode()) from error

    request('/api/auth/login', {key: credentials[key] for key in ('username', 'password')})
    existing = request('/api/repositories')
    ids = []
    for name, url in EXAMPLES:
        match = next((repo for repo in existing if repo['url'] == url), None)
        result = match or request('/api/repositories', {'name': name, 'url': url})
        ids.append(result['id'])
        print('已接入：' + name, flush=True)
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        repositories = [repo for repo in request('/api/repositories') if repo['id'] in ids]
        if all(repo['sync_state'] not in ('queued', 'running') for repo in repositories):
            break
        time.sleep(2)
    results = []
    for repo in repositories:
        if repo['sync_state'] != 'ready':
            raise RuntimeError(repo['name'] + ': ' + repo['sync_state'] + ': ' + repo['error'])
        detail = request('/api/repositories/' + repo['id'])
        result = {key: detail[key] for key in ('id', 'name', 'url', 'branch', 'commit_id', 'statistics')}
        results.append(result)
        print(f"验证通过：{repo['name']}，{detail['statistics']['files']} 文件，{detail['statistics']['symbols']} 符号，{detail['statistics']['relations']} 关系", flush=True)
    search = request('/api/search?' + urllib.parse.urlencode({'q': '路由 route'}))
    if not search['results']:
        raise RuntimeError('检索未返回任何真实来源')
    results.append({'search_evidence': [{key: item[key] for key in ('repository_id', 'path', 'start_line', 'end_line', 'commit_id', 'source_url')}
                                       for item in search['results'][:5]]})
    (DATA / 'demo-results.json').write_text(json.dumps(results, ensure_ascii=False, indent=2))
    print('真实仓库验收结果已保存：.data/demo-results.json')


if __name__ == '__main__':
    main()
