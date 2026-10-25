"""Durable sync queue and atomically published repository snapshots."""
from collections import Counter
import fcntl
import json
from pathlib import PurePosixPath
import re
import threading
import time

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

from .git_source import GitSource, redact
from .scanner import scan
from .store import identifier

SCANNER_VERSION = '3'


class Engine:
    def __init__(self, store):
        self.store = store
        self.stop_event = threading.Event()
        self.wake = threading.Event()
        self.git = GitSource(store.root, self.stop_event)
        self.thread = None
        self.lock = None

    def start(self):
        self.lock = (self.store.root / 'server.lock').open('a')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            self.lock.close()
            raise RuntimeError('此数据目录已有实例在运行；本版请使用单进程启动') from error
        with self.store.db(write=True) as con:
            interrupted = con.execute("SELECT id FROM repositories WHERE sync_state='running'").fetchall()
            for repo in interrupted:
                con.execute("UPDATE repositories SET sync_state='queued' WHERE id=?", (repo['id'],))
                con.execute("""UPDATE jobs SET state='interrupted', finished_at=?, error='进程重启，已重新排队'
                    WHERE repository_id=? AND state='running'""", (time.time(), repo['id']))
                con.execute('INSERT INTO jobs(id,repository_id,state,created_at) VALUES (?,?,?,?)',
                            (identifier(), repo['id'], 'queued', time.time()))
        self.thread = threading.Thread(target=self.loop, name='repository-indexer', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        self.wake.set()
        if self.thread:
            self.thread.join(timeout=10)
        elif self.lock:
            self.lock.close()

    def loop(self):
        try:
            while not self.stop_event.is_set():
                try:
                    worked = self.tick()
                except Exception:
                    # Database errors must not permanently kill the persistent queue.
                    import logging
                    logging.getLogger('repoknow').exception('索引队列失败')
                    worked = False
                if not worked:
                    self.wake.wait(2)
                    self.wake.clear()
        finally:
            # A slow scanner may outlive stop()'s grace period. Keep exclusivity
            # until its last database operation has actually finished.
            self.lock.close()

    def tick(self):
        with self.store.db(write=True) as con:
            row = con.execute('''SELECT r.*, s.commit_id, s.statistics FROM repositories r
                LEFT JOIN snapshots s ON s.id=r.current_snapshot
                WHERE r.sync_state='queued' ORDER BY r.created_at LIMIT 1''').fetchone()
            if not row:
                return False
            repo = dict(row)
            job = con.execute("""SELECT id FROM jobs WHERE repository_id=? AND state='queued'
                ORDER BY created_at LIMIT 1""", (repo['id'],)).fetchone()
            job_id = job['id'] if job else identifier()
            if not job:
                con.execute('INSERT INTO jobs(id,repository_id,state,created_at) VALUES (?,?,?,?)',
                            (job_id, repo['id'], 'queued', time.time()))
            con.execute("UPDATE repositories SET sync_state='running',error='' WHERE id=?", (repo['id'],))
            con.execute("UPDATE jobs SET state='running',started_at=? WHERE id=?", (time.time(), job_id))
        credentials = {}
        try:
            credentials = self.store.repository_credentials(repo)
            directory, branch, commit = self.git.fetch(repo, credentials)
            if self.stop_event.is_set():
                raise RuntimeError('索引已取消')
            previous = json.loads(repo['statistics'] or '{}')
            if commit == repo['commit_id'] and previous.get('scanner_version') == SCANNER_VERSION:
                self.finish_unchanged(repo['id'], job_id, commit)
                return True
            snapshot = identifier()
            files, symbols, relations, chunks, statistics = scan(self.git, directory, commit, snapshot)
            if self.stop_event.is_set():
                raise RuntimeError('索引已取消')
            statistics['scanner_version'] = SCANNER_VERSION
            self.publish(repo['id'], job_id, branch, commit, snapshot,
                         files, symbols, relations, chunks, statistics)
        except Exception as error:
            message = redact(str(error), credentials)
            with self.store.db(write=True) as con:
                state = 'queued' if self.stop_event.is_set() else 'failed'
                con.execute('UPDATE repositories SET sync_state=?,error=?,checked_at=? WHERE id=?',
                            (state, message, time.time(), repo['id']))
                con.execute('UPDATE jobs SET state=?,error=?,finished_at=? WHERE id=?',
                            ('interrupted' if self.stop_event.is_set() else 'failed', message, time.time(), job_id))
        return True

    def finish_unchanged(self, repo_id, job_id, commit):
        with self.store.db(write=True) as con:
            con.execute("UPDATE repositories SET sync_state='ready',error='',checked_at=? WHERE id=?",
                        (time.time(), repo_id))
            con.execute("UPDATE jobs SET state='unchanged',finished_at=?,commit_id=? WHERE id=?",
                        (time.time(), commit, job_id))

    def publish(self, repo_id, job_id, branch, commit, snapshot, files, symbols, relations, chunks, statistics):
        now = time.time()
        with self.store.db(write=True) as con:
            con.execute('INSERT INTO snapshots VALUES (?,?,?,?,?)',
                        (snapshot, repo_id, commit, now, json.dumps(statistics)))
            con.executemany('INSERT INTO files VALUES (?,?,?,?,?,?,?)', [
                (snapshot, f['path'], f['language'], f['kind'], f['content'], f['sha'], f['line_count']) for f in files])
            con.executemany('INSERT INTO symbols VALUES (?,?,?,?,?,?,?,?,?)', [
                (s['id'], snapshot, s['path'], s['name'], s['qualified_name'], s['kind'],
                 s['start_line'], s['end_line'], s['signature']) for s in symbols])
            con.executemany('''INSERT INTO relations
                (snapshot_id,source_id,target_id,kind,confidence,path,line) VALUES (?,?,?,?,?,?,?)''', [
                (snapshot, r['source_id'], r['target_id'], r['kind'], r['confidence'], r['path'], r['line']) for r in relations])
            con.executemany('''INSERT INTO chunks
                (snapshot_id,path,start_line,end_line,kind,name,text,search_text) VALUES (?,?,?,?,?,?,?,?)''', [
                (snapshot, c['path'], c['start_line'], c['end_line'], c['kind'], c['name'], c['text'], c['search_text']) for c in chunks])
            con.execute("""UPDATE repositories SET current_snapshot=?,branch=?,sync_state='ready',
                error='',checked_at=? WHERE id=?""", (snapshot, branch, now, repo_id))
            con.execute("UPDATE jobs SET state='ready',finished_at=?,commit_id=?,summary=? WHERE id=?",
                        (now, commit, json.dumps(statistics), job_id))
            # Publication and old-generation cleanup are in one transaction.
            con.execute('DELETE FROM snapshots WHERE repository_id=? AND id!=?', (repo_id, snapshot))


def toml_dependencies(name, content):
    document = tomllib.loads(content)
    result = []
    if name == 'pyproject.toml':
        project = document.get('project', {})
        lists = []
        if isinstance(project, dict):
            lists.append(project.get('dependencies', []))
            optional = project.get('optional-dependencies', {})
            if isinstance(optional, dict):
                lists.extend(optional.values())
        groups = document.get('dependency-groups', {})
        if isinstance(groups, dict):
            # All groups are inspected; include-group references need no recursion.
            lists.extend(groups.values())
        for requirements in lists:
            if not isinstance(requirements, list):
                continue
            for requirement in requirements:
                match = re.match(r'^([A-Za-z0-9][A-Za-z0-9._-]*)(.*)$', requirement.strip()) if isinstance(requirement, str) else None
                if match:
                    result.append((match[1], match[2].strip()))
        tool = document.get('tool', {})
        poetry = tool.get('poetry', {}) if isinstance(tool, dict) else {}
        if isinstance(poetry, dict):
            tables = [poetry.get('dependencies', {}), poetry.get('dev-dependencies', {})]
            groups = poetry.get('group', {})
            if isinstance(groups, dict):
                tables.extend(group.get('dependencies', {}) for group in groups.values() if isinstance(group, dict))
            for table in tables:
                if not isinstance(table, dict):
                    continue
                for package, spec in table.items():
                    if package.lower() == 'python':
                        continue
                    if isinstance(spec, str):
                        result.append((package, spec))
                    elif isinstance(spec, dict):
                        result.append((package, str(spec.get('version', ''))))
    elif name == 'Cargo.toml':
        scopes = [document]
        workspace = document.get('workspace', {})
        if isinstance(workspace, dict):
            scopes.append(workspace)
        targets = document.get('target', {})
        if isinstance(targets, dict):
            scopes.extend(scope for scope in targets.values() if isinstance(scope, dict))
        for scope in scopes:
            for group in ('dependencies', 'dev-dependencies', 'build-dependencies'):
                table = scope.get(group, {})
                if not isinstance(table, dict):
                    continue
                for package, spec in table.items():
                    if isinstance(spec, str):
                        result.append((package, spec))
                    elif isinstance(spec, dict):
                        version = spec.get('version')
                        if not isinstance(version, str):
                            version = next((key for key in ('workspace', 'path', 'git') if spec.get(key)), '')
                        result.append((package, version))
    return result


def dependencies(files, limit=100):
    result = []
    for file in files:
        path, content = file['path'], file['content']
        name = PurePosixPath(path).name
        if name == 'package.json':
            try:
                document = json.loads(content)
                for group in ('dependencies', 'devDependencies'):
                    for package, version in document.get(group, {}).items():
                        result.append({'name': package, 'version': str(version), 'path': path})
            except (ValueError, AttributeError):
                continue
        elif name.startswith('requirements') and name.endswith('.txt'):
            for line in content.splitlines():
                value = line.strip()
                if value and not value.startswith(('#', '-', 'http', 'git+')):
                    match = re.match(r'([\w.-]+)(.*)', value)
                    if match:
                        result.append({'name': match[1], 'version': match[2].strip(), 'path': path})
        elif name == 'go.mod':
            for package, version in re.findall(r'(?m)^\s*(?:require\s+)?([\w.-]+/[\w./-]+)\s+(v[^\s]+)', content):
                result.append({'name': package, 'version': version, 'path': path})
        elif name in ('pyproject.toml', 'Cargo.toml'):
            try:
                result.extend({'name': package, 'version': version, 'path': path}
                              for package, version in toml_dependencies(name, content))
            except ValueError:
                continue
    return result[:limit]


def overview(files):
    modules = Counter(PurePosixPath(file['path']).parts[0] if '/' in file['path'] else '根目录' for file in files)
    candidates = {'main.go', 'main.py', 'manage.py', 'app.py', 'server.py', 'server.js',
                  'index.js', 'index.ts', 'main.ts', 'Dockerfile', 'Makefile'}
    entrypoints = [file['path'] for file in files if PurePosixPath(file['path']).name in candidates]
    packages = dependencies(files, limit=101)
    return {'modules': [{'name': name, 'files': count} for name, count in modules.most_common(30)],
            'entrypoints': entrypoints[:30], 'dependencies': packages[:100],
            'truncated': {'modules': len(modules) > 30, 'entrypoints': len(entrypoints) > 30,
                          'dependencies': len(packages) > 100}}
