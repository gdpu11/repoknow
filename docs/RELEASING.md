# Preparing a source release

This procedure prepares local artifacts. It does not commit, push, tag, create a remote repository, upload a release, or deploy anything.

## Sanitize and export

```bash
python3 scripts/prepare_release.py --check
python3 scripts/prepare_release.py
```

The exporter uses `release-manifest.json` as an explicit allowlist. It excludes private data, environment files, credentials, Git metadata/caches, logs, backups, virtual environments and bytecode. It rejects symlinks, non-UTF-8 payloads and common credential/private-path patterns. If `.data/admin.json` exists, its password and read-only Key are also compared against the candidate source files without printing them.

Findings include a relative path, line and rule name, never the matched secret. Unexpected top-level files fail the check rather than silently entering a release. A clean scan is useful evidence, not a guarantee that arbitrary sensitive business information has been recognized; review the file inventory before publishing.

Default local output:

```text
.data/release/repoknow-0.2.0-source.zip
.data/release/repoknow-0.2.0-source.zip.sha256
.data/release/source-release-report.json
```

The ZIP contains a `MANIFEST.sha256` for all payload files. Timestamps and permissions are normalized, and unchanged source produces identical ZIP bytes. The external SHA-256 covers the complete ZIP.
The generated checksum manifest is recognized when checking an extracted archive; re-exporting regenerates it instead of treating it as source or nesting a previous manifest.

After extracting the archive, run the checks in [VALIDATION.md](VALIDATION.md). Verify a fresh environment rather than relying only on an existing developer installation.

## Maintainer decisions before publication

- Choose the owning GitHub account/organization and repository name; add actual project URLs after that choice. Do not publish placeholder owner URLs.
- Confirm the [MIT license](../LICENSE) and original-source ownership, and recheck [dependency notices](../THIRD_PARTY_NOTICES.md) after lock updates.
- Enable GitHub private vulnerability reporting before accepting public usage; no maintainer email is invented in this source.
- Review `README.md`, the source inventory, CI output and the archive checksum.
- Obtain explicit authorization for commit/push/tag/release/deployment. Preparation alone is not publication authorization.

Do not upload `.data/`, authenticated MCP client configuration, imported repository contents, local screenshots or logs. Source release tooling does not rotate or revoke previously exposed credentials.
