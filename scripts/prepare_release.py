"""Validate and export an allowlisted source archive; never publish or run Git."""
import argparse
import fnmatch
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tempfile
import zipfile


EXCLUDED_DIRS = {'.git', '.data', '.venv', '.codex', '.cursor', '.aws', 'dist',
                 '__pycache__', 'node_modules', '.pytest_cache', '.mypy_cache', '.ruff_cache', 'htmlcov'}
EXCLUDED_FILES = {'.DS_Store', 'admin.json', 'credentials.json', 'credentials.key', 'auth.json', 'MANIFEST.sha256'}
EXCLUDED_PATTERNS = ('*.pyc', '*.log', '*.sqlite*', '*.db', '*.key', '*.pem', '*.p12', '*.pfx', '*.bak', '.coverage*')
RULES = {
    'personal_absolute_path': re.compile(r'(?i)/(?:Users|home)/[A-Za-z0-9_.-]+/'),
    'windows_personal_path': re.compile(r'(?i)\b[A-Z]:\\Users\\[A-Za-z0-9_.-]+\\'),
    'private_key': re.compile(r'-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY-----'),
    'provider_token': re.compile(r'\b(?:gh[pousr]_|github_pat_|glpat-)[A-Za-z0-9_-]{20,}'),
    'ai_api_key': re.compile(r'\bsk-(?:proj-)?[A-Za-z0-9_-]{24,}'),
    'aws_access_key': re.compile(r'\b(?:AKIA|ASIA)[A-Z0-9]{16}\b'),
    'credential_in_url': re.compile(r'https?://[^\s/\x22\x27:]+:[^\s/\x22\x27@]+@'),
    'internal_hostname': re.compile(r'(?i)\b(?:[a-z0-9-]+\.)+(?:corp|internal|lan)\b'),
    'private_ipv4': re.compile(r'\b(?:10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|172\.(?:1[6-9]|2\d|3[01])(?:\.\d{1,3}){2})\b'),
}


def forbidden(path):
    return (any(part in EXCLUDED_DIRS or part.startswith('.venv') for part in path.parts)
            or path.name in EXCLUDED_FILES
            or (path.name.startswith('.env') and path.name != '.env.example')
            or any(fnmatch.fnmatch(path.name, pattern) for pattern in EXCLUDED_PATTERNS))


def relative_path(value):
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or '..' in path.parts or '\\' in value:
        raise ValueError('Allowlist entries must be relative paths without traversal')
    return path


def collect_files(root):
    root = Path(root).resolve()
    manifest = json.loads((root / 'release-manifest.json').read_text())
    entries = manifest['files'] + manifest['directories']
    paths = [relative_path(value) for value in entries]
    allowed_roots = {path.parts[0] for path in paths}
    for entry in root.iterdir():
        if entry.name not in allowed_roots and not forbidden(PurePosixPath(entry.name)):
            raise ValueError('Unexpected top-level entry; review the allowlist: ' + entry.name)
    selected = set()

    def include(path):
        relative = PurePosixPath(path.relative_to(root).as_posix())
        if forbidden(relative):
            return
        if path.is_symlink():
            raise ValueError('Symlink not allowed in source archive: ' + str(relative))
        if not path.resolve().is_relative_to(root):
            raise ValueError('Path escapes source directory: ' + str(relative))
        if path.is_file():
            selected.add(relative.as_posix())

    for value in manifest['files']:
        relative = relative_path(value)
        if forbidden(relative):
            raise ValueError('Private/generated file explicitly listed: ' + str(relative))
        path = root / relative
        if not path.is_file():
            raise ValueError('Required public file missing: ' + str(relative))
        include(path)
    for value in manifest['directories']:
        relative = relative_path(value)
        if forbidden(relative):
            raise ValueError('Private/generated directory explicitly listed: ' + str(relative))
        directory = root / relative
        if not directory.is_dir() or directory.is_symlink():
            raise ValueError('Public directory missing or symlinked: ' + str(relative))
        for path in directory.rglob('*'):
            include(path)
    return sorted(selected)


def local_secrets(root):
    account_path = Path(root) / '.data/admin.json'
    if not account_path.exists():
        return []
    account = json.loads(account_path.read_text())
    return [account[key] for key in ('password', 'read_only_api_key')
            if isinstance(account.get(key), str) and len(account[key]) >= 10]


def inspect_payload(root, paths, secrets=()):
    findings, payload = [], {}
    for relative in paths:
        raw = (Path(root) / relative).read_bytes()
        try:
            text = raw.decode('utf-8')
        except UnicodeDecodeError:
            findings.append({'path': relative, 'line': 1, 'rule': 'non_utf8_payload'})
            continue
        if '\x00' in text:
            findings.append({'path': relative, 'line': 1, 'rule': 'binary_payload'})
            continue
        for rule, pattern in RULES.items():
            for match in pattern.finditer(text):
                findings.append({'path': relative, 'line': text.count('\n', 0, match.start()) + 1, 'rule': rule})
        for secret in secrets:
            if secret and secret in text:
                findings.append({'path': relative, 'line': text.count('\n', 0, text.index(secret)) + 1,
                                 'rule': 'known_local_secret'})
        payload[relative] = raw
    return findings, payload


def export_archive(payload, output, name, version):
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name + '-' + version):
        raise ValueError('Invalid archive name/version')
    output = Path(output)
    if output.suffix != '.zip':
        raise ValueError('Source output must be a .zip archive')
    output.parent.mkdir(parents=True, exist_ok=True)
    prefix = name + '-' + version
    checksums = {path: hashlib.sha256(raw).hexdigest() for path, raw in payload.items()}
    checksum_text = ''.join(f'{digest}  {path}\n' for path, digest in sorted(checksums.items()))
    with tempfile.NamedTemporaryFile(dir=output.parent, suffix='.zip', delete=False) as temporary:
        temporary_path = Path(temporary.name)
    try:
        with zipfile.ZipFile(temporary_path, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for relative, raw in sorted((payload | {'MANIFEST.sha256': checksum_text.encode()}).items()):
                info = zipfile.ZipInfo(prefix + '/' + relative, date_time=(1980, 1, 1, 0, 0, 0))
                info.create_system = 3
                info.external_attr = 0o100644 << 16
                archive.writestr(info, raw, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
        with zipfile.ZipFile(temporary_path) as archive:
            if archive.testzip():
                raise ValueError('Archive CRC verification failed')
            for relative, digest in checksums.items():
                if hashlib.sha256(archive.read(prefix + '/' + relative)).hexdigest() != digest:
                    raise ValueError('Archive payload verification failed: ' + relative)
        temporary_path.replace(output)
    finally:
        temporary_path.unlink(missing_ok=True)
    digest = hashlib.sha256(output.read_bytes()).hexdigest()
    output.with_suffix(output.suffix + '.sha256').write_text(digest + '  ' + output.name + '\n')
    report = {'name': name, 'version': version, 'payload_files': len(payload),
              'archive': output.name, 'archive_bytes': output.stat().st_size,
              'archive_sha256': digest, 'findings': [], 'files': checksums}
    (output.parent / 'source-release-report.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Only validate; do not create an archive')
    parser.add_argument('--output', type=Path, help='Output .zip path (default: .data/release/)')
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    try:
        paths = collect_files(root)
        findings, payload = inspect_payload(root, paths, local_secrets(root))
        if findings:
            print(json.dumps({'checked_files': len(paths), 'findings': findings}, indent=2))
            raise SystemExit(1)
        if args.check:
            print(json.dumps({'checked_files': len(paths), 'findings': []}))
            return
        metadata = json.loads((root / 'release-manifest.json').read_text())
        version = re.search(r'^version\s*=\s*"([^"]+)"', (root / 'pyproject.toml').read_text(), re.M)[1]
        output = args.output or root / '.data/release' / (metadata['name'] + '-' + version + '-source.zip')
        report = export_archive(payload, output, metadata['name'], version)
        print(json.dumps({k: v for k, v in report.items() if k != 'files'}, indent=2))
    except (ValueError, KeyError, OSError) as error:
        raise SystemExit(str(error)) from None


if __name__ == '__main__':
    main()
