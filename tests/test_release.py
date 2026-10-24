import hashlib
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from scripts.prepare_release import collect_files, export_archive, inspect_payload, local_secrets


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / 'app').mkdir()
        (self.root / 'README.md').write_text('# Public project\n')
        (self.root / 'app/main.py').write_text('print("public source")\n')
        self.manifest = {'name': 'fixture', 'files': ['README.md', 'release-manifest.json'], 'directories': ['app']}
        self.save_manifest()

    def tearDown(self):
        self.temp.cleanup()

    def save_manifest(self):
        (self.root / 'release-manifest.json').write_text(json.dumps(self.manifest))

    def test_private_runtime_data_and_caches_are_not_selected(self):
        for folder in ('.data', '.venv', '.git', 'app/__pycache__'):
            target = self.root / folder
            target.mkdir(parents=True)
            (target / 'private.txt').write_text('private runtime fixture')
        (self.root / '.env.production').write_text('fixture environment values')
        (self.root / 'app/runtime.log').write_text('fixture log')
        (self.root / 'app/credentials.key').write_text('fixture key material')
        self.assertEqual(collect_files(self.root), ['README.md', 'app/main.py', 'release-manifest.json'])

    def test_explicit_private_allowlist_entry_is_rejected(self):
        (self.root / '.data').mkdir()
        (self.root / '.data/admin.json').write_text('{}')
        self.manifest['files'].append('.data/admin.json')
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'Private/generated'):
            collect_files(self.root)

    def test_unreviewed_root_file_is_rejected(self):
        (self.root / 'personal-notes.md').write_text('unreviewed fixture')
        with self.assertRaisesRegex(ValueError, 'Unexpected top-level'):
            collect_files(self.root)

    def test_traversal_is_rejected(self):
        self.manifest['files'].append('../outside.txt')
        self.save_manifest()
        with self.assertRaisesRegex(ValueError, 'relative paths'):
            collect_files(self.root)

    def test_symlink_cannot_export_external_content(self):
        (self.root / 'app/linked.py').symlink_to(self.root.parent / 'outside-fixture.py')
        with self.assertRaisesRegex(ValueError, 'Symlink'):
            collect_files(self.root)

    def test_findings_never_echo_matched_secrets(self):
        secret = 'glpat-' + 'fixture' * 5
        personal_path = '/Users/' + 'private-fixture-user' + '/project'
        (self.root / 'app/main.py').write_text(secret + '\n' + personal_path)
        findings, _ = inspect_payload(self.root, collect_files(self.root))
        self.assertEqual({f['rule'] for f in findings}, {'provider_token', 'personal_absolute_path'})
        self.assertNotIn(secret, json.dumps(findings))
        self.assertNotIn(personal_path, json.dumps(findings))

    def test_generated_local_credentials_are_compared_without_output(self):
        (self.root / '.data').mkdir()
        secret = 'fixture-random-administrator-password'
        (self.root / '.data/admin.json').write_text(json.dumps({'password': secret, 'read_only_api_key': 'fixture-readonly-secret-key'}))
        (self.root / 'README.md').write_text('accidental leak: ' + secret)
        findings, _ = inspect_payload(self.root, collect_files(self.root), local_secrets(self.root))
        self.assertEqual(findings[0]['rule'], 'known_local_secret')
        self.assertNotIn(secret, json.dumps(findings))

    def test_archive_is_reproducible_and_manifest_matches_payload(self):
        findings, payload = inspect_payload(self.root, collect_files(self.root))
        self.assertEqual(findings, [])
        first = export_archive(payload, self.root / '.data/first.zip', 'fixture', '0.1.0')
        second = export_archive(payload, self.root / '.data/second.zip', 'fixture', '0.1.0')
        self.assertEqual(first['archive_sha256'], second['archive_sha256'])
        with zipfile.ZipFile(self.root / '.data/first.zip') as archive:
            manifest = archive.read('fixture-0.1.0/MANIFEST.sha256').decode()
            self.assertEqual(len(archive.namelist()), len(payload) + 1)
            for line in manifest.splitlines():
                digest, path = line.split('  ', 1)
                self.assertEqual(hashlib.sha256(archive.read('fixture-0.1.0/' + path)).hexdigest(), digest)
            extracted = self.root / '.data/extracted'
            archive.extractall(extracted)
        source = extracted / 'fixture-0.1.0'
        findings, restored = inspect_payload(source, collect_files(source))
        self.assertEqual(findings, [])
        self.assertEqual(restored, payload)
        rebuilt = export_archive(restored, self.root / '.data/rebuilt.zip', 'fixture', '0.1.0')
        self.assertEqual(rebuilt['archive_sha256'], first['archive_sha256'])


if __name__ == '__main__':
    unittest.main()
