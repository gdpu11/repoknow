# Changelog

## 0.2.0 — 2026-10-25 (public alpha)

- Close public registration by default; allow explicit opt-in with `REPOKNOW_ALLOW_REGISTRATION=1`.
- Add administrator-only member creation, enable/disable controls, and session/Key revocation on disable. Preserve repository data and credential material.
- Migrate the original bootstrap account to administrator without resetting passwords, Keys, or indexes.
- Add GitHub organization and Enterprise discovery/import through the Web UI, local command, and authenticated APIs, with pagination, same-origin credentials, and idempotent queues.
- Validate provider ownership and clone paths before enqueueing batch imports. Keep read-only MCP credentials out of account management.

## 0.1.0 — 2026-10-24 (initial public alpha)

- Use the RepoKnow name across the Web UI, MCP server, Python project, container configuration, and source archive.
- Import public and authorized private GitHub/GitLab repositories over HTTP(S).
- Discover GitLab group/subgroup repositories with pagination and idempotent imports.
- Reuse encrypted per-user platform credentials with owner and origin checks.
- Index immutable Git snapshots, documents, Tree-sitter symbols, and static call candidates.
- Search across repositories with SQLite FTS5 and source provenance.
- Expose five read-only MCP tools and a Web UI.
- Preserve published indexes on sync failure and recover interrupted jobs.
- Provide local management, import commands, container configuration, and a sanitized source exporter.

This is an early alpha release for local or controlled private deployments. It is not a production support guarantee.
