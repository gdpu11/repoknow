"""Read immutable Git blobs; never check out or execute repository code."""
import os
from pathlib import Path
import re
import signal
import subprocess
import time
from urllib.parse import urlsplit, urlunsplit


def origin_url(raw):
    raw = raw.strip()
    if any(ord(c) < 32 for c in raw):
        raise ValueError('地址不能包含控制字符')
    parsed = urlsplit(raw if '://' in raw else 'https://' + raw)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('请输入 HTTP(S) Git 平台地址')
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError('地址不能包含凭证、查询参数或片段')
    try:
        parsed.port
    except ValueError as error:
        raise ValueError('端口无效') from error
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), '', '', ''))


def normalize_url(raw):
    raw = raw.strip().rstrip('/')
    parsed = urlsplit(raw)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise ValueError('请输入 GitHub / GitLab 的 HTTP(S) 克隆地址')
    if parsed.username is not None or parsed.password is not None:
        raise ValueError('请在凭证字段填写账号和 Token，不要放进仓库地址')
    if parsed.query or parsed.fragment or any(ord(c) < 32 for c in raw):
        raise ValueError('仓库地址不能包含查询参数、片段或控制字符')
    try:
        parsed.port
    except ValueError as error:
        raise ValueError('仓库端口无效') from error
    path = parsed.path.rstrip('/')
    if len([part for part in path.split('/') if part]) < 2:
        raise ValueError('地址需要包含组织和仓库名称')
    if any(part in ('.', '..') for part in path.split('/')):
        raise ValueError('仓库地址包含无效路径')
    if path.endswith('.git'):
        path = path[:-4]
    platform = 'github' if parsed.hostname.lower() == 'github.com' else 'gitlab'
    return urlunsplit((parsed.scheme, parsed.netloc.lower(), path + '.git', '', '')), platform


def source_url(url, platform, commit, path, line=1, end=None):
    from urllib.parse import quote
    base = url.removesuffix('.git')
    route = '/blob/' if platform == 'github' else '/-/blob/'
    anchor = f'#L{line}'
    if end and end != line:
        anchor += f'-L{end}' if platform == 'github' else f'-{end}'
    return base + route + commit + '/' + quote(path, safe='/') + anchor


def redact(message, credentials):
    for value in (credentials.get('token'), credentials.get('username')):
        if value and len(value) > 2:
            message = message.replace(value, '[已隐藏]')
    message = re.sub(r'https?://[^\s/@]+:[^\s/@]+@', 'https://[已隐藏]@', message)
    return ' '.join(message.split())[:500]


class GitSource:
    def __init__(self, root, stop_event):
        self.root = Path(root)
        self.stop_event = stop_event
        self.askpass = self.root / 'repoknow-git-askpass.sh'
        helper = '''#!/bin/sh
case "$1" in
  *Username*|*username*) printf '%s' "$REPOKNOW_GIT_USERNAME" ;;
  *) printf '%s' "$REPOKNOW_GIT_TOKEN" ;;
esac
'''
        try:
            fd = os.open(self.askpass, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'w') as stream:
                stream.write(helper)

    def run(self, args, directory=None, credentials=None, data=None, timeout=180):
        credentials = credentials or {}
        env = {k: v for k, v in os.environ.items() if not k.startswith('GIT_')}
        env.update(GIT_TERMINAL_PROMPT='0', GIT_CONFIG_NOSYSTEM='1',
                   GIT_CONFIG_GLOBAL=os.devnull, GIT_ASKPASS=str(self.askpass),
                   REPOKNOW_GIT_USERNAME=credentials.get('username') or 'oauth2',
                   REPOKNOW_GIT_TOKEN=credentials.get('token') or '')
        options = ['git', '-c', 'core.hooksPath=' + os.devnull,
                   '-c', 'protocol.ext.allow=never', '-c', 'protocol.file.allow=never',
                   '-c', 'http.followRedirects=false', '-c', 'credential.helper=',
                   '-c', 'gc.auto=0', '-c', 'maintenance.auto=false']
        if directory:
            options.append('--git-dir=' + str(directory))
        process = subprocess.Popen(options + args, env=env, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   start_new_session=True)
        deadline, first = time.monotonic() + timeout, True
        try:
            while True:
                try:
                    out, err = process.communicate(input=data if first else None, timeout=0.5)
                    break
                except subprocess.TimeoutExpired:
                    first = False
                    if self.stop_event.is_set() or time.monotonic() >= deadline:
                        raise RuntimeError('Git 同步已取消或超时')
            if process.returncode:
                raise RuntimeError('Git 操作失败：' + redact(err.decode(errors='replace'), credentials))
            return out
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate()

    def fetch(self, repo, credentials):
        directory = self.root / 'repositories' / (repo['id'] + '.git')
        if not (directory / 'HEAD').exists():
            directory.parent.mkdir(parents=True, exist_ok=True)
            self.run(['init', '--bare', str(directory)])
        branch = repo['branch']
        if not branch:
            refs = self.run(['ls-remote', '--symref', repo['url'], 'HEAD'], credentials=credentials)
            match = re.search(rb'^ref: refs/heads/(.+)\tHEAD$', refs, re.M)
            if not match:
                raise RuntimeError('未找到默认分支，请手动填写分支名称')
            branch = match[1].decode()
        if branch.startswith('-') or any(ord(c) < 32 for c in branch):
            raise ValueError('分支名称无效')
        self.run(['check-ref-format', '--branch', branch])
        self.run(['fetch', '--force', '--no-tags', '--depth=1', '--', repo['url'],
                  'refs/heads/' + branch + ':refs/heads/repoknow'], directory, credentials)
        commit = self.run(['rev-parse', '--verify', 'refs/heads/repoknow^{commit}'], directory).decode().strip()
        return directory, branch, commit

    def files(self, directory, commit):
        raw = self.run(['ls-tree', '-r', '-z', '--long', commit], directory)
        rows = []
        for item in raw.split(b'\0'):
            if not item:
                continue
            info, path = item.split(b'\t', 1)
            mode, kind, sha, size = info.split()
            rows.append({'path': path.decode('utf-8', errors='replace'),
                         'mode': mode.decode(), 'sha': sha.decode(),
                         'size': int(size) if size != b'-' else 0, 'type': kind.decode()})
        return rows

    def blobs(self, directory, entries):
        raw = self.run(['cat-file', '--batch'], directory,
                       data=''.join(item['sha'] + '\n' for item in entries).encode())
        offset = 0
        for entry in entries:
            stop = raw.index(b'\n', offset)
            sha, kind, size = raw[offset:stop].split()
            if sha.decode() != entry['sha'] or kind != b'blob':
                raise RuntimeError('Git 对象读取结果不一致')
            length = int(size)
            yield entry, raw[stop + 1:stop + 1 + length]
            offset = stop + 1 + length + 1
