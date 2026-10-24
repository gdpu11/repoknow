"""Discover GitLab groups and GitHub organizations without redirects or code execution."""
import json
import re
import time
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .git_source import normalize_url, origin_url
from . import __version__


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def get_json(url, token='', platform='gitlab'):
    label = 'GitHub' if platform == 'github' else 'GitLab'
    headers = {'Accept': 'application/json', 'User-Agent': 'RepoKnow/' + __version__}
    if platform == 'github':
        headers.update(Accept='application/vnd.github+json', **{'X-GitHub-Api-Version': '2022-11-28'})
    if token:
        headers['Authorization' if platform == 'github' else 'PRIVATE-TOKEN'] = (
            'Bearer ' + token if platform == 'github' else token)
    try:
        with build_opener(NoRedirect()).open(Request(url, headers=headers), timeout=15) as response:
            raw = response.read(4 * 1024 * 1024 + 1)
            if len(raw) > 4 * 1024 * 1024:
                raise ValueError(label + ' API 单页响应过大，尚未导入任何仓库')
            return json.loads(raw), response.headers.get('X-Next-Page', '')
    except HTTPError as error:
        messages = {
            401: 'GitLab Token 无效或已过期',
            403: 'GitLab 拒绝访问，请检查 Token 的 read_api 权限和组访问权限',
            404: 'GitLab 组不存在或当前账号不可见；组地址不能是个人用户主页',
            429: 'GitLab API 请求受限，请稍后重试',
        }
        if platform == 'github':
            messages = {401: 'GitHub Token 无效或已过期',
                        403: 'GitHub 拒绝访问或 API 限流，请检查 Token 权限、组织授权和请求额度',
                        404: 'GitHub 组织不存在或当前 Token 不可见；个人主页不是组织',
                        429: 'GitHub API 请求受限，请稍后重试'}
        raise ValueError(messages.get(error.code, f'{label} API 返回 HTTP {error.code}，尚未导入')) from None
    except UnicodeEncodeError:
        raise ValueError(label + ' API 地址或 Token 包含无效字符') from None
    except (URLError, TimeoutError, OSError):
        raise ValueError('无法访问 ' + label + ' API，请检查网络和 TLS 证书') from None
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise ValueError(label + ' API 未返回有效 JSON') from None


def normalize_group(raw):
    raw = raw.strip()
    if '://' not in raw:
        raw = 'https://' + raw
    base = origin_url(raw)
    parsed = urlsplit(raw)
    path = unquote(parsed.path).strip('/')
    if (not path or path.endswith('.git') or
            any(part in ('', '.', '..', '-') for part in path.split('/')) or
            any(ord(c) < 32 for c in path)):
        raise ValueError('请输入 GitLab 组或子组主页地址，例如 https://gitlab.example.com/engineering')
    return base, path


def discover_group(url, credentials=None, include_subgroups=True):
    base, path = normalize_group(url)
    token = (credentials or {}).get('token', '')
    group, _ = get_json(base + '/api/v4/groups/' + quote(path, safe='') + '?with_projects=false', token)
    if (not isinstance(group, dict) or not isinstance(group.get('id'), int)
            or not isinstance(group.get('full_path'), str) or not group['full_path']):
        raise ValueError('GitLab 组信息格式无效')
    namespace = group['full_path']
    projects, skipped, seen = [], [], set()
    deadline = time.monotonic() + 120
    for page in range(1, 52):
        if time.monotonic() > deadline:
            raise ValueError('发现组仓库超时，尚未导入任何仓库，请缩小到子组')
        query = urlencode({'include_subgroups': str(include_subgroups).lower(),
                           'with_shared': 'false', 'per_page': 100,
                           'page': page, 'order_by': 'id', 'sort': 'asc'})
        rows, next_page = get_json(f'{base}/api/v4/groups/{group["id"]}/projects?{query}', token)
        if not isinstance(rows, list):
            raise ValueError('GitLab 仓库列表格式无效')
        for project in rows:
            if not isinstance(project, dict) or not isinstance(project.get('path_with_namespace'), str):
                raise ValueError('GitLab 仓库信息格式无效')
            name = project.get('path_with_namespace', '')
            if not name.startswith(namespace + '/'):
                raise ValueError('GitLab 返回了组范围以外的仓库，已停止导入')
            if not isinstance(project.get('http_url_to_repo'), str):
                raise ValueError('GitLab 仓库缺少 HTTP(S) 克隆地址')
            clone_url, _ = normalize_url(project['http_url_to_repo'])
            if origin_url(clone_url) != base:
                raise ValueError('GitLab 返回了其他域名的克隆地址，凭证未发送，已停止导入')
            if unquote(urlsplit(clone_url).path).strip('/').removesuffix('.git') != name:
                raise ValueError('GitLab 克隆地址与仓库路径不匹配，已停止导入')
            if clone_url in seen:
                continue
            seen.add(clone_url)
            if len(seen) > 5000:
                raise ValueError('组超过本版 5000 仓库上限，尚未导入任何仓库，请使用子组地址')
            if project.get('empty_repo') or not project.get('default_branch'):
                skipped.append({'name': name, 'reason': '空仓库或尚无默认分支'})
                continue
            if not isinstance(project['default_branch'], str) or len(project['default_branch']) > 200:
                raise ValueError('GitLab 默认分支信息无效')
            projects.append({'name': name, 'url': clone_url, 'branch': project['default_branch']})
        if next_page:
            if not next_page.isdigit() or int(next_page) != page + 1:
                raise ValueError('GitLab 分页信息异常，尚未导入任何仓库')
        elif len(rows) < 100:
            return {'url': base + '/' + namespace, 'name': namespace,
                    'projects': projects, 'skipped': skipped,
                    'include_subgroups': include_subgroups}
    raise ValueError('组分页超过扫描上限，尚未导入任何仓库，请使用子组地址')


def normalize_organization(raw):
    raw = raw.strip()
    if '://' not in raw:
        raw = 'https://' + raw
    base = origin_url(raw)
    name = unquote(urlsplit(raw).path).strip('/')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]*', name):
        raise ValueError('请输入 GitHub 组织主页，例如 https://github.com/example-org')
    return base, name


def discover_organization(url, credentials=None):
    base, name = normalize_organization(url)
    api_base = 'https://api.github.com' if base == 'https://github.com' else base + '/api/v3'
    token = (credentials or {}).get('token', '')
    organization, _ = get_json(api_base + '/orgs/' + quote(name, safe=''), token, platform='github')
    if (not isinstance(organization, dict) or organization.get('type') != 'Organization'
            or not isinstance(organization.get('login'), str)
            or organization['login'].casefold() != name.casefold()):
        raise ValueError('GitHub 返回的组织身份与地址不匹配；不支持个人用户主页')
    namespace = organization['login']
    projects, skipped, seen = [], [], set()
    deadline = time.monotonic() + 120
    for page in range(1, 52):
        if time.monotonic() > deadline:
            raise ValueError('GitHub 组织发现超时，尚未导入任何仓库')
        query = urlencode({'type': 'all', 'per_page': 100, 'page': page,
                           'sort': 'full_name', 'direction': 'asc'})
        rows, _ = get_json(api_base + '/orgs/' + quote(namespace, safe='') + '/repos?' + query,
                           token, platform='github')
        if not isinstance(rows, list):
            raise ValueError('GitHub 仓库列表格式无效')
        for project in rows:
            if not isinstance(project, dict) or not isinstance(project.get('full_name'), str):
                raise ValueError('GitHub 仓库信息格式无效')
            full_name = project['full_name']
            parts = full_name.split('/')
            owner = project.get('owner')
            if (len(parts) != 2 or not parts[1] or parts[0].casefold() != namespace.casefold()
                    or not isinstance(owner, dict) or not isinstance(owner.get('login'), str)
                    or owner['login'].casefold() != namespace.casefold()):
                raise ValueError('GitHub 返回了组织范围以外的仓库，已停止导入')
            if not isinstance(project.get('clone_url'), str):
                raise ValueError('GitHub 仓库缺少 HTTP(S) 克隆地址')
            clone_url, _ = normalize_url(project['clone_url'])
            if (origin_url(clone_url) != base or
                    unquote(urlsplit(clone_url).path).strip('/').removesuffix('.git').casefold() != full_name.casefold()):
                raise ValueError('GitHub 克隆地址与组织仓库不匹配，凭证未发送，已停止导入')
            key = clone_url.casefold()
            if key in seen:
                continue
            seen.add(key)
            if len(seen) > 5000:
                raise ValueError('GitHub 组织超过本版 5000 仓库上限，尚未导入任何仓库')
            branch = project.get('default_branch')
            if branch is None or branch == '':
                skipped.append({'name': full_name, 'reason': '尚无默认分支'})
                continue
            if not isinstance(branch, str) or len(branch) > 200:
                raise ValueError('GitHub 默认分支信息无效')
            projects.append({'name': full_name, 'url': clone_url, 'branch': branch})
        # Never follow provider-supplied next URLs with a private Token.
        if len(rows) < 100:
            return {'url': base + '/' + namespace, 'name': namespace,
                    'projects': projects, 'skipped': skipped}
    raise ValueError('GitHub 组织分页超过扫描上限，尚未导入任何仓库')
