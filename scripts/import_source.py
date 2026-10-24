"""Import an authorized repository or GitLab group through the running local API."""
import argparse
import http.cookiejar
import json
import os
from pathlib import Path
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, HTTPCookieProcessor, Request, build_opener

ROOT = Path(__file__).resolve().parents[1]


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('url', help='GitHub/GitLab 仓库或 GitLab 组地址')
    parser.add_argument('--kind', choices=['auto', 'repository', 'group'], default='auto')
    parser.add_argument('--connection', default='', help='已在界面配置的平台凭证名称或 ID；默认按域名复用')
    parser.add_argument('--branch', default='', help='单仓库分支，留空检测默认分支')
    parser.add_argument('--no-subgroups', action='store_true', help='组导入不包含子组')
    parser.add_argument('--wait', action='store_true', help='等待本批仓库索引完成')
    parser.add_argument('--timeout', type=int, default=600, help='等待索引的秒数，默认 600')
    args = parser.parse_args()
    base = os.environ.get('REPOKNOW_URL', 'http://127.0.0.1:8090').rstrip('/')
    # This helper reads a local administrator password. Never send it to a remote URL.
    if urlsplit(base).hostname not in ('127.0.0.1', 'localhost', '::1'):
        raise SystemExit('本地导入命令只允许连接回环地址，未发送管理员凭证')
    data = Path(os.environ.get('REPOKNOW_DATA_DIR', ROOT / '.data'))
    account = json.loads((data / 'admin.json').read_text())
    client = build_opener(NoRedirect(), HTTPCookieProcessor(http.cookiejar.CookieJar()))

    def request(path, body=None):
        req = Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                      headers={'Content-Type': 'application/json', 'X-Requested-With': 'RepoKnow'})
        try:
            with client.open(req, timeout=150) as response:
                return json.load(response)
        except HTTPError as error:
            payload = json.loads(error.read())
            raise ValueError(f'HTTP {error.code}: {payload.get("detail", "请求失败")}') from None

    try:
        request('/api/auth/login', {key: account[key] for key in ('username', 'password')})
        owner_id = request('/api/auth/me')['id']
        connection_id = ''
        if args.connection:
            matches = [c for c in request('/api/connections')
                       if args.connection in (c['name'], c['id'])]
            if len(matches) != 1:
                raise ValueError('平台凭证名称不存在或不唯一，请在界面确认后使用 ID')
            connection_id = matches[0]['id']
        url = args.url if '://' in args.url else 'https://' + args.url
        kind = args.kind
        if kind == 'auto':
            path = urlsplit(url).path.strip('/')
            kind = 'group' if path and '/' not in path and not path.endswith('.git') else 'repository'
        if kind == 'group':
            result = request('/api/groups/import', {'url': url, 'connection_id': connection_id,
                             'include_subgroups': not args.no_subgroups})
            ids = {row['id'] for row in result['repositories']}
        else:
            normalized = url.rstrip('/').removesuffix('.git') + '.git'
            existing = next((repo for repo in request('/api/repositories')
                             if repo['owner_id'] == owner_id and repo['url'] == normalized
                             and (not args.branch or repo['branch'] == args.branch)), None)
            result = existing or request('/api/repositories', {'url': url, 'branch': args.branch,
                                                               'connection_id': connection_id})
            ids = {result['id']}
        print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
        if args.wait and ids:
            deadline = time.monotonic() + args.timeout
            while True:
                repos = [r for r in request('/api/repositories') if r['id'] in ids]
                if len(repos) != len(ids):
                    raise ValueError('部分仓库已不可见，无法确认完成状态')
                if all(r['sync_state'] not in ('queued', 'running') for r in repos):
                    summary = [{key: r[key] for key in ('id', 'name', 'sync_state', 'error', 'commit_id')}
                               for r in repos]
                    print(json.dumps({'index_results': summary}, ensure_ascii=False, indent=2))
                    if any(r['sync_state'] != 'ready' for r in repos):
                        raise ValueError('部分仓库索引失败，详见上面的逐仓库结果')
                    break
                if time.monotonic() >= deadline:
                    raise ValueError('等待超时，后台任务继续执行，可在知识库界面查看状态')
                time.sleep(2)
    except (ValueError, URLError, TimeoutError) as error:
        raise SystemExit(str(error)) from None


if __name__ == '__main__':
    main()
