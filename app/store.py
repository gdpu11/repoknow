"""SQLite persistence, encrypted Git credentials and local authentication."""
from contextlib import closing, contextmanager
import hashlib
import hmac
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
import uuid

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.exceptions import InvalidTag

from .git_source import origin_url


def identifier():
    return uuid.uuid4().hex


def password_hash(password, salt=None):
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=16384, r=8, p=1, dklen=32)
    return salt.hex() + ':' + digest.hex()


def check_password(password, encoded):
    salt, _ = encoded.split(':')
    return hmac.compare_digest(password_hash(password, bytes.fromhex(salt)), encoded)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
 id TEXT PRIMARY KEY, username TEXT NOT NULL UNIQUE COLLATE NOCASE,
 password TEXT NOT NULL, created_at REAL NOT NULL,
 is_admin INTEGER NOT NULL DEFAULT 0, disabled INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS sessions (
 hash TEXT PRIMARY KEY, user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
 kind TEXT NOT NULL DEFAULT 'web', expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_keys (
 id TEXT PRIMARY KEY,
 session_hash TEXT NOT NULL UNIQUE REFERENCES sessions(hash) ON DELETE CASCADE,
 name TEXT NOT NULL, created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS connections (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
 base_url TEXT NOT NULL, platform TEXT NOT NULL, credential BLOB, created_at REAL NOT NULL,
 UNIQUE(owner_id, base_url)
);
CREATE TABLE IF NOT EXISTS repositories (
 id TEXT PRIMARY KEY, owner_id TEXT NOT NULL REFERENCES users(id), name TEXT NOT NULL,
 url TEXT NOT NULL, platform TEXT NOT NULL, branch TEXT NOT NULL DEFAULT '',
 credential BLOB, connection_id TEXT REFERENCES connections(id), visibility TEXT NOT NULL DEFAULT 'private',
 current_snapshot TEXT REFERENCES snapshots(id), sync_state TEXT NOT NULL DEFAULT 'queued',
 error TEXT NOT NULL DEFAULT '', created_at REAL NOT NULL, checked_at REAL,
 UNIQUE(owner_id, url, branch)
);
CREATE TABLE IF NOT EXISTS snapshots (
 id TEXT PRIMARY KEY, repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
 commit_id TEXT NOT NULL, indexed_at REAL NOT NULL, statistics TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
 snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
 path TEXT NOT NULL, language TEXT NOT NULL, kind TEXT NOT NULL,
 content TEXT NOT NULL, sha TEXT NOT NULL, line_count INTEGER NOT NULL,
 PRIMARY KEY(snapshot_id, path)
);
CREATE TABLE IF NOT EXISTS symbols (
 id TEXT PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
 path TEXT NOT NULL, name TEXT NOT NULL, qualified_name TEXT NOT NULL,
 kind TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
 signature TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS symbols_snapshot ON symbols(snapshot_id, name);
CREATE TABLE IF NOT EXISTS relations (
 id INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
 source_id TEXT NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
 target_id TEXT NOT NULL REFERENCES symbols(id) ON DELETE CASCADE,
 kind TEXT NOT NULL, confidence REAL NOT NULL, path TEXT NOT NULL, line INTEGER NOT NULL,
 UNIQUE(source_id, target_id, kind, line)
);
CREATE TABLE IF NOT EXISTS chunks (
 id INTEGER PRIMARY KEY, snapshot_id TEXT NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
 path TEXT NOT NULL, start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
 kind TEXT NOT NULL, name TEXT NOT NULL, text TEXT NOT NULL, search_text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS chunks_snapshot ON chunks(snapshot_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
 search_text, content='chunks', content_rowid='id', tokenize='unicode61'
);
CREATE TRIGGER IF NOT EXISTS chunks_insert AFTER INSERT ON chunks BEGIN
 INSERT INTO chunks_fts(rowid, search_text) VALUES (new.id, new.search_text);
END;
CREATE TRIGGER IF NOT EXISTS chunks_delete AFTER DELETE ON chunks BEGIN
 INSERT INTO chunks_fts(chunks_fts, rowid, search_text) VALUES ('delete', old.id, old.search_text);
END;
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, repository_id TEXT NOT NULL REFERENCES repositories(id) ON DELETE CASCADE,
 state TEXT NOT NULL, created_at REAL NOT NULL, started_at REAL, finished_at REAL,
 commit_id TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS jobs_repository ON jobs(repository_id, created_at DESC);
"""


class Store:
    def __init__(self, directory):
        self.root = Path(directory).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        self.path = self.root / 'repoknow.sqlite3'
        key_path = self.root / 'credentials.key'
        try:
            fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            pass
        else:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(AESGCM.generate_key(bit_length=256))
        self.cipher = AESGCM(key_path.read_bytes())
        with closing(sqlite3.connect(self.path)) as con:
            con.execute('PRAGMA journal_mode=WAL')
            con.executescript(SCHEMA)
            con.execute('BEGIN IMMEDIATE')
            user_columns = {row[1] for row in con.execute('PRAGMA table_info(users)')}
            if 'is_admin' not in user_columns:
                con.execute('ALTER TABLE users ADD COLUMN is_admin INTEGER NOT NULL DEFAULT 0')
                # Version 0.1 always created the bootstrap administrator first.
                con.execute('''UPDATE users SET is_admin=1 WHERE id=(
                    SELECT id FROM users ORDER BY created_at,id LIMIT 1)''')
            if 'disabled' not in user_columns:
                con.execute('ALTER TABLE users ADD COLUMN disabled INTEGER NOT NULL DEFAULT 0')
            if 'connection_id' not in {row[1] for row in con.execute('PRAGMA table_info(repositories)')}:
                con.execute('ALTER TABLE repositories ADD COLUMN connection_id TEXT REFERENCES connections(id)')
            # Preserve existing Key hashes and clients; only add revocation metadata.
            con.execute('''INSERT INTO api_keys(id,session_hash,name,created_at)
                SELECT lower(hex(randomblob(16))),s.hash,'已有只读 Key',0
                FROM sessions s WHERE s.kind='api' AND NOT EXISTS (
                    SELECT 1 FROM api_keys k WHERE k.session_hash=s.hash)''')
            con.commit()
        os.chmod(self.path, 0o600)
        self.bootstrap()

    @contextmanager
    def db(self, write=False):
        con = sqlite3.connect(self.path, timeout=30)
        con.row_factory = sqlite3.Row
        con.execute('PRAGMA foreign_keys=ON')
        try:
            con.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield con
            if write:
                con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()

    def encrypt(self, repo_id, username, token):
        if not username and not token:
            return None
        nonce = secrets.token_bytes(12)
        raw = json.dumps({'username': username, 'token': token}).encode()
        return nonce + self.cipher.encrypt(nonce, raw, repo_id.encode())

    def decrypt(self, repo_id, value):
        if not value:
            return {'username': '', 'token': ''}
        try:
            return json.loads(self.cipher.decrypt(value[:12], value[12:], repo_id.encode()))
        except InvalidTag as error:
            raise ValueError('仓库凭证解密失败，请重新填写凭证；恢复备份时需同时恢复 credentials.key') from error

    def bootstrap(self):
        with self.db(write=True) as con:
            if con.execute('SELECT 1 FROM users LIMIT 1').fetchone():
                return
            user_id, password = identifier(), secrets.token_urlsafe(20)
            con.execute('INSERT INTO users(id,username,password,created_at,is_admin) VALUES (?, ?, ?, ?, 1)',
                        (user_id, 'admin', password_hash(password), time.time()))
            key = self.create_api_key(con, user_id, '初始管理员 Key')
            target = self.root / 'admin.json'
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as stream:
                json.dump({'username': 'admin', 'password': password,
                           'read_only_api_key': key['key']}, stream, indent=2)

    def create_api_key(self, con, user_id, name):
        token = secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        key_id, created_at = identifier(), time.time()
        con.execute('INSERT INTO sessions VALUES (?,?,?,?)', (digest, user_id, 'api', 0))
        con.execute('INSERT INTO api_keys VALUES (?,?,?,?)', (key_id, digest, name, created_at))
        return {'id': key_id, 'name': name, 'created_at': created_at, 'key': token}

    def authenticate(self, token):
        if not token:
            return None
        digest = hashlib.sha256(token.encode()).hexdigest()
        with self.db() as con:
            row = con.execute('''SELECT u.id, u.username, u.is_admin, s.kind, s.hash AS session_hash FROM sessions s
                JOIN users u ON u.id=s.user_id
                WHERE s.hash=? AND u.disabled=0 AND (s.expires_at=0 OR s.expires_at>?)''',
                              (digest, time.time())).fetchone()
        return dict(row) if row else None

    def login(self, username, password):
        with self.db(write=True) as con:
            user = con.execute('SELECT * FROM users WHERE username=?', (username,)).fetchone()
            if not user or user['disabled'] or not check_password(password, user['password']):
                return None
            token = secrets.token_urlsafe(32)
            con.execute('DELETE FROM sessions WHERE expires_at>0 AND expires_at<?', (time.time(),))
            con.execute('INSERT INTO sessions VALUES (?, ?, ?, ?)', (
                hashlib.sha256(token.encode()).hexdigest(), user['id'], 'web', time.time() + 86400 * 7))
        return token

    def register(self, username, password):
        user_id = identifier()
        with self.db(write=True) as con:
            con.execute('INSERT INTO users(id,username,password,created_at) VALUES (?, ?, ?, ?)',
                        (user_id, username, password_hash(password), time.time()))
        return user_id

    def connection(self, con, user_id, url, connection_id=''):
        origin = origin_url(url)
        if connection_id:
            row = con.execute('SELECT * FROM connections WHERE id=? AND owner_id=?',
                              (connection_id, user_id)).fetchone()
            if not row or row['base_url'] != origin:
                raise ValueError('平台凭证不存在、无权使用，或与目标仓库域名不匹配')
        else:
            row = con.execute('SELECT * FROM connections WHERE owner_id=? AND base_url=?',
                              (user_id, origin)).fetchone()
        return dict(row) if row else None

    def repository_credentials(self, repo):
        if repo.get('connection_id'):
            with self.db() as con:
                connection = self.connection(con, repo['owner_id'], repo['url'], repo['connection_id'])
            return self.decrypt(connection['id'], connection['credential'])
        return self.decrypt(repo['id'], repo['credential'])

    def repository(self, repo_id, user_id, owner=False, connection=None):
        def query(con):
            return con.execute('''SELECT r.*, s.commit_id, s.indexed_at, s.statistics
                FROM repositories r LEFT JOIN snapshots s ON s.id=r.current_snapshot
                WHERE r.id=? AND (r.owner_id=? OR (r.visibility='shared' AND ?=0))''',
                              (repo_id, user_id, int(owner))).fetchone()
        if connection is not None:
            row = query(connection)
        else:
            with self.db() as con:
                row = query(con)
        return dict(row) if row else None

    def queue(self, repo_id):
        with self.db(write=True) as con:
            changed = con.execute("""UPDATE repositories SET sync_state='queued', error=''
                WHERE id=? AND sync_state NOT IN ('queued', 'running')""", (repo_id,)).rowcount
            if changed:
                con.execute('INSERT INTO jobs(id,repository_id,state,created_at) VALUES (?,?,?,?)',
                            (identifier(), repo_id, 'queued', time.time()))
        return bool(changed)

    def public_repository(self, row):
        result = {key: row[key] for key in (
            'id', 'owner_id', 'name', 'url', 'platform', 'branch', 'visibility',
            'connection_id',
            'current_snapshot', 'sync_state', 'error', 'created_at', 'checked_at',
            'commit_id', 'indexed_at')}
        result['has_credentials'] = bool(row['credential'] or row['connection_id'])
        result['statistics'] = json.loads(row['statistics'] or '{}')
        return result
