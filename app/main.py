"""Authenticated Web API and read-only MCP interface."""
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import time
import threading
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from markdown_it import MarkdownIt
from pydantic import BaseModel, Field

from . import __version__
from .engine import Engine, overview
from .git_source import normalize_url, origin_url, source_url
from .middleware import RequestSizeLimit
from .providers import discover_group, normalize_group, discover_organization, normalize_organization
from .scanner import query_terms
from .store import Store, identifier


class AccountInput(BaseModel):
    username: str = Field(min_length=2, max_length=64, pattern=r'^[\w.@-]+$')
    password: str = Field(min_length=10, max_length=200)


class AccountAccessInput(BaseModel):
    disabled: bool


class RepositoryInput(BaseModel):
    url: str = Field(min_length=5, max_length=1000)
    name: str = Field(default='', max_length=1000)
    branch: str = Field(default='', max_length=200)
    platform: str = Field(default='auto', pattern='^(auto|github|gitlab)$')
    username: str = Field(default='', max_length=200)
    token: str = Field(default='', max_length=4096)
    connection_id: str = Field(default='', max_length=64)
    visibility: str = Field(default='private', pattern='^(private|shared)$')


class CredentialInput(BaseModel):
    username: str = Field(default='', max_length=200)
    token: str = Field(default='', max_length=4096)


class ConnectionInput(CredentialInput):
    name: str = Field(default='', max_length=100)
    base_url: str = Field(min_length=5, max_length=1000)
    platform: str = Field(default='gitlab', pattern='^(github|gitlab)$')


class ConnectionUpdate(CredentialInput):
    name: str = Field(default='', max_length=100)


class NamespaceInput(BaseModel):
    url: str = Field(min_length=5, max_length=1000)
    connection_id: str = Field(default='', max_length=64)
    visibility: str = Field(default='private', pattern='^(private|shared)$')


class GroupInput(NamespaceInput):
    include_subgroups: bool = True


class SourceInput(BaseModel):
    repository_id: str = Field(min_length=1, max_length=64)
    commit: str = Field(pattern=r'^[0-9a-f]{40}(?:[0-9a-f]{24})?$')
    path: str = Field(min_length=1, max_length=2000)
    start_line: int = Field(default=1, ge=1)
    end_line: int = Field(default=100, ge=1)


class SymbolInput(BaseModel):
    repository_id: str = Field(min_length=1, max_length=64)
    q: str = Field(default='', max_length=200)


class TraceInput(BaseModel):
    repository_id: str = Field(min_length=1, max_length=64)
    symbol_id: str = Field(min_length=1, max_length=64)


class SearchInput(BaseModel):
    q: str = Field(min_length=1, max_length=500)
    repository_id: str = Field(default='', max_length=64)
    kind: str = Field(default='', pattern='^(|code|document|symbol)$')
    limit: int = Field(default=30, ge=1, le=100)


def create_app(data_dir=None, start_worker=True):
    store = Store(data_dir or os.environ.get('REPOKNOW_DATA_DIR', '.data'))
    registration_enabled = os.environ.get('REPOKNOW_ALLOW_REGISTRATION', '0') == '1'
    engine = Engine(store)
    markdown = MarkdownIt('commonmark', {'html': False})
    markdown.enable(['table', 'strikethrough'])
    markdown.disable('image')

    @asynccontextmanager
    async def lifespan(app):
        if start_worker:
            engine.start()
        yield
        if start_worker:
            engine.stop()

    app = FastAPI(title='RepoKnow', version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.store, app.state.engine = store, engine
    app.add_middleware(RequestSizeLimit)
    login_attempts = {}
    login_lock = threading.Lock()

    @app.middleware('http')
    async def browser_boundary(request, call_next):
        if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
            origin = request.headers.get('origin')
            if origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
                return JSONResponse({'detail': '来源校验失败'}, status_code=403)
            if not request.headers.get('authorization') and request.headers.get('x-requested-with') != 'RepoKnow':
                return JSONResponse({'detail': '缺少请求校验标记'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['Referrer-Policy'] = 'no-referrer'
        response.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; "
            "img-src 'self' data:; connect-src 'self'; object-src 'none'; "
            "frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
        response.headers['Cache-Control'] = 'no-store'
        return response

    def principal(request: Request):
        bearer = request.headers.get('authorization', '')
        token = bearer[7:] if bearer.lower().startswith('bearer ') else request.cookies.get('repoknow_session')
        user = store.authenticate(token)
        if not user:
            raise HTTPException(401, '请先登录')
        if user['kind'] == 'api' and request.method not in ('GET', 'HEAD') and request.url.path != '/mcp':
            raise HTTPException(403, '此 API Key 仅允许只读查询')
        return user

    def administrator(user=Depends(principal)):
        if not user['is_admin'] or user['kind'] != 'web':
            raise HTTPException(403, '此操作需要管理员登录；只读 API Key 不能管理成员')
        return user

    def repository(con, repo_id, user, owner=False):
        result = store.repository(repo_id, user['id'], owner, connection=con)
        if not result:
            raise HTTPException(404, '仓库不存在或无权访问')
        return result

    @app.get('/health')
    def health():
        return {'status': 'ok', 'version': __version__}

    @app.get('/api/auth/config')
    def authentication_config():
        return {'registration_enabled': registration_enabled}

    @app.post('/api/auth/login')
    def login(body: AccountInput, request: Request, response: Response):
        key = request.client.host if request.client else 'local'
        now = time.time()
        with login_lock:
            attempts = [stamp for stamp in login_attempts.get(key, []) if stamp > now - 60]
            if len(attempts) >= 10:
                raise HTTPException(429, '尝试过于频繁，请一分钟后重试')
            login_attempts[key] = attempts + [now]
        token = store.login(body.username, body.password)
        if not token:
            raise HTTPException(401, '账号或密码错误')
        response.set_cookie('repoknow_session', token, httponly=True, samesite='strict',
                            secure=request.url.scheme == 'https', max_age=86400 * 7)
        return {'username': body.username}

    @app.post('/api/auth/register', status_code=201)
    def register(body: AccountInput):
        if not registration_enabled:
            raise HTTPException(403, '当前实例已关闭公开注册，请联系管理员创建账户')
        try:
            store.register(body.username, body.password)
        except sqlite3.IntegrityError:
            raise HTTPException(409, '账号已存在')
        return {'username': body.username}

    @app.get('/api/auth/me')
    def me(user=Depends(principal)):
        return {'id': user['id'], 'username': user['username'], 'is_admin': bool(user['is_admin'])}

    def public_account(row):
        return {'id': row['id'], 'username': row['username'], 'created_at': row['created_at'],
                'is_admin': bool(row['is_admin']), 'disabled': bool(row['disabled'])}

    @app.get('/api/admin/users')
    def list_accounts(user=Depends(administrator)):
        with store.db() as con:
            return [public_account(row) for row in con.execute(
                'SELECT id,username,created_at,is_admin,disabled FROM users ORDER BY created_at,id')]

    @app.post('/api/admin/users', status_code=201)
    def add_account(body: AccountInput, user=Depends(administrator)):
        try:
            user_id = store.register(body.username, body.password)
        except sqlite3.IntegrityError:
            raise HTTPException(409, '账号已存在')
        with store.db() as con:
            return public_account(con.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone())

    @app.put('/api/admin/users/{user_id}')
    def update_account_access(user_id: str, body: AccountAccessInput, user=Depends(administrator)):
        with store.db(write=True) as con:
            row = con.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
            if not row:
                raise HTTPException(404, '账号不存在')
            if body.disabled and row['is_admin']:
                raise HTTPException(422, '管理员账号不能停用')
            con.execute('UPDATE users SET disabled=? WHERE id=?', (int(body.disabled), user_id))
            if body.disabled:
                con.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
        return {'ok': True}

    @app.post('/api/auth/logout')
    def logout(request: Request, response: Response, user=Depends(principal)):
        token = request.cookies.get('repoknow_session', '')
        with store.db(write=True) as con:
            con.execute('DELETE FROM sessions WHERE hash=?', (hashlib.sha256(token.encode()).hexdigest(),))
        response.delete_cookie('repoknow_session')
        return {'ok': True}

    @app.get('/api/repositories')
    def repositories(user=Depends(principal)):
        with store.db() as con:
            rows = con.execute('''SELECT r.*,s.commit_id,s.indexed_at,s.statistics
                FROM repositories r LEFT JOIN snapshots s ON s.id=r.current_snapshot
                WHERE r.owner_id=? OR r.visibility='shared' ORDER BY r.created_at DESC''', (user['id'],)).fetchall()
        return [store.public_repository(dict(row)) for row in rows]

    def public_connection(row):
        return {key: row[key] for key in ('id', 'name', 'base_url', 'platform', 'created_at')} | {
            'has_credentials': bool(row['credential'])}

    @app.get('/api/connections')
    def connections(user=Depends(principal)):
        with store.db() as con:
            return [public_connection(dict(row)) for row in con.execute(
                'SELECT * FROM connections WHERE owner_id=? ORDER BY name', (user['id'],))]

    @app.post('/api/connections', status_code=201)
    def add_connection(body: ConnectionInput, user=Depends(principal)):
        try:
            base = origin_url(body.base_url)
            if urlsplit(body.base_url if '://' in body.base_url else 'https://' + body.base_url).path not in ('', '/'):
                raise ValueError('平台地址只填域名，例如 https://gitlab.com；组地址在导入时填写')
            if any(ord(c) < 32 for c in body.username + body.token):
                raise ValueError('凭证包含无效字符')
            uid = identifier()
            with store.db(write=True) as con:
                con.execute('INSERT INTO connections VALUES (?,?,?,?,?,?,?)',
                    (uid, user['id'], body.name.strip() or urlsplit(base).netloc, base, body.platform,
                     store.encrypt(uid, body.username, body.token), time.time()))
                row = dict(con.execute('SELECT * FROM connections WHERE id=?', (uid,)).fetchone())
            return public_connection(row)
        except ValueError as error:
            raise HTTPException(422, str(error))
        except sqlite3.IntegrityError:
            raise HTTPException(409, '该平台已配置，请更新已有凭证')

    @app.put('/api/connections/{connection_id}')
    def update_connection(connection_id: str, body: ConnectionUpdate, user=Depends(principal)):
        if any(ord(c) < 32 for c in body.username + body.token):
            raise HTTPException(422, '凭证包含无效字符')
        with store.db(write=True) as con:
            row = con.execute('SELECT * FROM connections WHERE id=? AND owner_id=?',
                              (connection_id, user['id'])).fetchone()
            if not row:
                raise HTTPException(404, '平台凭证不存在或无权修改')
            try:
                try:
                    previous = store.decrypt(connection_id, row['credential'])
                except ValueError:
                    if not body.token:
                        raise
                    previous = {}
                credential = store.encrypt(connection_id, body.username or previous.get('username', ''),
                                           body.token or previous.get('token', ''))
            except ValueError as error:
                raise HTTPException(422, str(error))
            con.execute('UPDATE connections SET name=?,credential=? WHERE id=?',
                        (body.name.strip() or row['name'], credential, connection_id))
        return {'ok': True}

    def enqueue_repository(con, body, user, skip_existing=False):
        try:
            url, platform = normalize_url(body.url)
            if body.branch.startswith('-') or any(ord(c) < 32 for c in body.branch + body.username + body.token):
                raise ValueError('分支或凭证包含无效字符')
        except ValueError as error:
            raise HTTPException(422, str(error))
        uid = identifier()
        name = body.name.strip() or urlsplit(url).path.rsplit('/', 1)[-1].removesuffix('.git')
        if body.connection_id and (body.username or body.token):
            raise HTTPException(422, '平台凭证与单仓库凭证请选择一种')
        try:
            connection = None if body.username or body.token else store.connection(con, user['id'], url, body.connection_id)
        except ValueError as error:
            raise HTTPException(422, str(error))
        credential = store.encrypt(uid, body.username, body.token)
        duplicate = con.execute('''SELECT id,sync_state FROM repositories WHERE owner_id=? AND url=?
                AND (branch=? OR branch='' OR ?='')''',
                (user['id'], url, body.branch.strip(), body.branch.strip())).fetchone()
        if duplicate:
            if skip_existing:
                return {'id': duplicate['id'], 'name': name, 'created': False, 'sync_state': duplicate['sync_state']}
            raise HTTPException(409, '该地址或分支已接入；添加其他分支时请填写明确的分支名')
        con.execute('''INSERT INTO repositories
                    (id,owner_id,name,url,platform,branch,credential,connection_id,visibility,created_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?)''', (uid, user['id'], name, url,
                    (connection['platform'] if connection else platform) if body.platform == 'auto' else body.platform,
                    body.branch.strip(), credential, connection['id'] if connection else None,
                    body.visibility, time.time()))
        con.execute('INSERT INTO jobs(id,repository_id,state,created_at) VALUES (?,?,?,?)',
                        (identifier(), uid, 'queued', time.time()))
        return {'id': uid, 'name': name, 'created': True, 'sync_state': 'queued'}

    @app.post('/api/repositories', status_code=201)
    def add_repository(body: RepositoryInput, user=Depends(principal)):
        with store.db(write=True) as con:
            result = enqueue_repository(con, body, user)
        engine.wake.set()
        return result

    def group_projects(body, user):
        try:
            base, _ = normalize_group(body.url)
            with store.db() as con:
                connection = store.connection(con, user['id'], base, body.connection_id)
            if connection and connection['platform'] != 'gitlab':
                raise ValueError('组发现当前支持 GitLab，请选择 GitLab 平台凭证')
            credentials = store.decrypt(connection['id'], connection['credential']) if connection else {}
            data = discover_group(body.url, credentials, body.include_subgroups)
            return data, connection
        except ValueError as error:
            raise HTTPException(422, str(error))

    @app.post('/api/groups/discover')
    def preview_group(body: GroupInput, user=Depends(principal)):
        data, _ = group_projects(body, user)
        return data

    @app.post('/api/groups/import')
    def import_group(body: GroupInput, user=Depends(principal)):
        data, connection = group_projects(body, user)
        return import_discovered(data, connection, body, user, 'gitlab')

    def import_discovered(data, connection, body, user, platform):
        results = []
        with store.db(write=True) as con:
            for project in data['projects']:
                entry = RepositoryInput(**project, platform=platform, visibility=body.visibility,
                                        connection_id=connection['id'] if connection else '')
                results.append(enqueue_repository(con, entry, user, skip_existing=True))
        engine.wake.set()
        return {('group' if platform == 'gitlab' else 'organization'): data['url'],
                'discovered': len(data['projects']) + len(data['skipped']),
                'created': sum(item['created'] for item in results),
                'existing': sum(not item['created'] for item in results),
                'skipped': data['skipped'], 'repositories': results}

    def organization_projects(body, user):
        try:
            base, _ = normalize_organization(body.url)
            with store.db() as con:
                connection = store.connection(con, user['id'], base, body.connection_id)
            if connection and connection['platform'] != 'github':
                raise ValueError('组织发现需要 GitHub 平台凭证；GitLab 请使用组导入')
            credentials = store.decrypt(connection['id'], connection['credential']) if connection else {}
            return discover_organization(body.url, credentials), connection
        except ValueError as error:
            raise HTTPException(422, str(error))

    @app.post('/api/organizations/discover')
    def preview_organization(body: NamespaceInput, user=Depends(principal)):
        data, _ = organization_projects(body, user)
        return data

    @app.post('/api/organizations/import')
    def import_organization(body: NamespaceInput, user=Depends(principal)):
        data, connection = organization_projects(body, user)
        return import_discovered(data, connection, body, user, 'github')

    @app.get('/api/repositories/{repo_id}')
    def repository_detail(repo_id: str, user=Depends(principal)):
        with store.db() as con:
            repo = repository(con, repo_id, user)
            files = [dict(row) for row in con.execute(
                'SELECT path,kind,language,line_count,content FROM files WHERE snapshot_id=? ORDER BY path',
                (repo['current_snapshot'],))]
            result = store.public_repository(repo)
            result['overview'] = overview(files)
            result['files'] = [{key: file[key] for key in ('path', 'kind', 'language', 'line_count')} for file in files]
        return result

    @app.post('/api/repositories/{repo_id}/sync')
    def sync(repo_id: str, user=Depends(principal)):
        with store.db() as con:
            repository(con, repo_id, user, owner=True)
        if not store.queue(repo_id):
            raise HTTPException(409, '仓库已在队列中')
        engine.wake.set()
        return {'sync_state': 'queued'}

    @app.put('/api/repositories/{repo_id}/credentials')
    def credentials(repo_id: str, body: CredentialInput, user=Depends(principal)):
        if any(ord(c) < 32 for c in body.username + body.token):
            raise HTTPException(422, '凭证包含无效控制字符')
        with store.db(write=True) as con:
            repository(con, repo_id, user, owner=True)
            con.execute('UPDATE repositories SET credential=?,connection_id=NULL WHERE id=?',
                        (store.encrypt(repo_id, body.username, body.token), repo_id))
        return {'ok': True}

    @app.get('/api/repositories/{repo_id}/jobs')
    def jobs(repo_id: str, user=Depends(principal)):
        with store.db() as con:
            repository(con, repo_id, user)
            return [dict(row) for row in con.execute('''SELECT id,state,created_at,started_at,
                finished_at,commit_id,error,summary FROM jobs WHERE repository_id=? ORDER BY created_at DESC LIMIT 30''', (repo_id,))]

    @app.get('/api/repositories/{repo_id}/file')
    def file_content(repo_id: str, path: str = Query(max_length=2000),
                     commit: str = Query(default='', pattern=r'^(?:[0-9a-f]{40}(?:[0-9a-f]{24})?)?$'),
                     user=Depends(principal)):
        with store.db() as con:
            repo = repository(con, repo_id, user)
            if commit and repo['commit_id'] != commit:
                raise HTTPException(409, '索引版本已更新，请重新打开仓库；不会替换为其他版本的同一行')
            row = con.execute('SELECT * FROM files WHERE snapshot_id=? AND path=?',
                              (repo['current_snapshot'], path)).fetchone()
            if not row:
                raise HTTPException(404, '该文件未被索引，或已被过滤')
            result = dict(row)
            result['commit_id'] = repo['commit_id']
            result['source_url'] = source_url(repo['url'], repo['platform'], repo['commit_id'], path)
            if result['language'] == 'Markdown':
                result['html'] = markdown.render(result['content'])
            return result

    def find_evidence(params, user):
        terms = query_terms(params.q)
        if not terms:
            return {'results': [], 'terms': [], 'total': 0}
        expression = ' OR '.join('"' + term.replace('"', '""') + '"' for term in terms)
        with store.db() as con:
            rows = con.execute('''SELECT c.path,c.start_line,c.end_line,c.kind,c.name,c.text,
                r.id AS repository_id,r.name AS repository_name,r.url,r.platform,s.commit_id,
                bm25(chunks_fts) AS rank
                FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid
                JOIN repositories r ON r.current_snapshot=c.snapshot_id
                JOIN snapshots s ON s.id=c.snapshot_id
                WHERE chunks_fts MATCH ? AND (r.owner_id=? OR r.visibility='shared')
                AND (?='' OR r.id=?) AND (?='' OR c.kind=?)
                ORDER BY rank LIMIT 300''', (expression, user['id'], params.repository_id,
                params.repository_id, params.kind, params.kind)).fetchall()
        ranked = []
        for row in rows:
            item = dict(row)
            name_match = params.q.lower() in item['name'].lower()
            item['score'] = -item.pop('rank') + (10 if name_match else 0)
            item['text_truncated'] = len(item['text']) > 16000
            item['text'] = item['text'][:16000]
            item['source_url'] = source_url(item.pop('url'), item.pop('platform'), item['commit_id'],
                                            item['path'], item['start_line'], item['end_line'])
            ranked.append(item)
        ranked.sort(key=lambda item: item['score'], reverse=True)
        deduped, seen = [], set()
        for item in ranked:
            # A function chunk and a covering file chunk may contain the same evidence.
            key = (item['repository_id'], item['path'], item['start_line'], item['end_line'])
            if key in seen:
                continue
            seen.add(key)
            deduped.append(item)
        return {'results': deduped[:params.limit], 'terms': terms,
                'total': len(deduped), 'truncated': len(rows) == 300}

    @app.get('/api/search')
    def search(q: str = Query(min_length=1, max_length=500), repository_id: str = '',
               kind: str = Query(default='', pattern='^(|code|document|symbol)$'),
               limit: int = Query(default=30, ge=1, le=100), user=Depends(principal)):
        return find_evidence(SearchInput(q=q, repository_id=repository_id, kind=kind, limit=limit), user)

    def lookup_symbols(repo_id, q, user, limit=300):
        with store.db() as con:
            repo = repository(con, repo_id, user)
            rows = con.execute('''SELECT * FROM symbols WHERE snapshot_id=? AND
                (name LIKE ? ESCAPE '\\' OR qualified_name LIKE ? ESCAPE '\\')
                ORDER BY path,start_line LIMIT ?''', (repo['current_snapshot'],
                '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%',
                '%' + q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_') + '%', limit)).fetchall()
            result = [dict(row) for row in rows]
            for item in result:
                item['source_url'] = source_url(repo['url'], repo['platform'], repo['commit_id'],
                                                item['path'], item['start_line'], item['end_line'])
            return {'symbols': result, 'commit_id': repo['commit_id'], 'truncated': len(result) == limit}

    @app.get('/api/repositories/{repo_id}/symbols')
    def symbols(repo_id: str, q: str = Query(default='', max_length=200), user=Depends(principal)):
        return lookup_symbols(repo_id, q, user)

    def trace(repo_id, symbol_id, user):
        with store.db() as con:
            repo = repository(con, repo_id, user)
            root = con.execute('SELECT * FROM symbols WHERE id=? AND snapshot_id=?',
                               (symbol_id, repo['current_snapshot'])).fetchone()
            if not root:
                raise HTTPException(404, '符号不存在')
            edges = [dict(row) for row in con.execute('''SELECT r.*,a.name AS source_name,b.name AS target_name
                FROM relations r JOIN symbols a ON a.id=r.source_id JOIN symbols b ON b.id=r.target_id
                WHERE r.snapshot_id=? AND (r.source_id=? OR r.target_id=?) LIMIT 101''',
                (repo['current_snapshot'], symbol_id, symbol_id))]
            for edge in edges:
                edge['source_url'] = source_url(repo['url'], repo['platform'], repo['commit_id'], edge['path'], edge['line'])
            return {'root': dict(root), 'commit_id': repo['commit_id'],
                    'relations': edges[:100], 'truncated': len(edges) > 100,
                    'note': '静态名称匹配：不解析动态分派、跨仓库调用或运行时依赖。'}

    @app.get('/api/repositories/{repo_id}/trace/{symbol_id}')
    def dependency_trace(repo_id: str, symbol_id: str, user=Depends(principal)):
        return trace(repo_id, symbol_id, user)

    def read_source(args, user):
        params = SourceInput.model_validate(args)
        repo_id, commit, path = params.repository_id, params.commit, params.path
        start, end = params.start_line, params.end_line
        if start < 1 or end < start or end - start >= 200:
            raise HTTPException(422, '每次最多读取 200 行')
        with store.db() as con:
            repo = repository(con, repo_id, user)
            if repo['commit_id'] != commit:
                raise HTTPException(409, '该 commit 不在当前索引；不会回退到最新分支')
            row = con.execute('SELECT content FROM files WHERE snapshot_id=? AND path=?',
                              (repo['current_snapshot'], path)).fetchone()
            if not row:
                raise HTTPException(404, '文件未索引或已被过滤')
            lines = row['content'].splitlines()
            if start > max(1, len(lines)):
                raise HTTPException(422, '起始行超过文件长度')
            selected = '\n'.join(lines[start - 1:end])
            raw = selected.encode()
            text = raw[:32768].decode(errors='ignore')
            actual_end = min(end, max(1, len(lines)), start + text.count('\n'))
            return {'repository_id': repo_id, 'commit': commit, 'path': path,
                    'start_line': start, 'end_line': actual_end,
                    'text': text, 'truncated': len(raw) > 32768,
                    'url': source_url(repo['url'], repo['platform'], commit, path, start, actual_end)}

    tools = [
        {'name': 'knowledge_catalog', 'description': '列出当前用户可访问的仓库及索引版本',
         'inputSchema': {'type': 'object', 'properties': {}}},
        {'name': 'knowledge_search', 'description': '检索源码和文档，返回路径、行号、commit 和来源链接',
         'inputSchema': SearchInput.model_json_schema()},
        {'name': 'knowledge_get_source', 'description': '按完整 commit 读取最多 200 行来源，不回退到其他版本',
         'inputSchema': SourceInput.model_json_schema()},
        {'name': 'knowledge_get_symbols', 'description': '查询仓库中的函数、类、方法定义',
         'inputSchema': SymbolInput.model_json_schema()},
        {'name': 'knowledge_trace_dependency', 'description': '查询一个符号的直接调用方和被调用方，含静态匹配置信度',
         'inputSchema': TraceInput.model_json_schema()},
    ]

    @app.post('/mcp')
    async def mcp(request: Request, user=Depends(principal)):
        try:
            body = await request.json()
            if not isinstance(body, dict) or body.get('jsonrpc') != '2.0':
                raise ValueError('invalid JSON-RPC request')
        except (ValueError, TypeError):
            return JSONResponse({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'Invalid request'}}, status_code=400)
        if 'id' not in body:
            return Response(status_code=202)
        method, params = body.get('method'), body.get('params') or {}
        if not isinstance(params, dict):
            return {'jsonrpc': '2.0', 'id': body['id'], 'error': {'code': -32602, 'message': 'Invalid params'}}
        if method == 'initialize':
            result = {'protocolVersion': params.get('protocolVersion', '2025-03-26'),
                      'capabilities': {'tools': {}}, 'serverInfo': {'name': 'repoknow', 'version': __version__}}
        elif method == 'ping':
            result = {}
        elif method == 'tools/list':
            result = {'tools': tools}
        elif method == 'tools/call':
            name, args = params.get('name'), params.get('arguments') or {}
            try:
                if name == 'knowledge_catalog':
                    data = repositories(user)
                elif name == 'knowledge_search':
                    data = find_evidence(SearchInput.model_validate(args), user)
                elif name == 'knowledge_get_source':
                    data = read_source(args, user)
                elif name == 'knowledge_get_symbols':
                    parsed = SymbolInput.model_validate(args)
                    data = lookup_symbols(parsed.repository_id, parsed.q, user)
                elif name == 'knowledge_trace_dependency':
                    parsed = TraceInput.model_validate(args)
                    data = trace(parsed.repository_id, parsed.symbol_id, user)
                else:
                    raise ValueError('Unknown tool')
                result = {'content': [{'type': 'text', 'text': json.dumps(data, ensure_ascii=False)}]}
            except (HTTPException, ValueError, TypeError, KeyError) as error:
                result = {'isError': True, 'content': [{'type': 'text', 'text': str(getattr(error, 'detail', error))}]}
        else:
            return {'jsonrpc': '2.0', 'id': body['id'], 'error': {'code': -32601, 'message': 'Method not found'}}
        return {'jsonrpc': '2.0', 'id': body['id'], 'result': result}

    static = Path(__file__).parent / 'static'
    static.mkdir(exist_ok=True)
    app.mount('/static', StaticFiles(directory=static), name='static')

    @app.get('/')
    def index():
        return FileResponse(static / 'index.html')

    return app
