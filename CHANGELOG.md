# Changelog

## Unreleased

- Index individual Han characters alongside bigrams while retaining bigram queries; prioritize exact name/text substrings before the search candidate limit. Add literal path-prefix and language filters to Web and MCP search.
- Add read-only MCP overview and directory tools, plus file-scoped symbol outlines and pagination with optional commit pinning. Expose truncation and next-page offsets.
- Extract PEP 621, Poetry, dependency-group and Cargo manifest dependencies without resolving or installing them. Use the standard TOML parser on Python 3.11+ and Tomli on 3.10.
- Increment the scanner version so an explicit sync rebuilds old Chinese indexes even at an unchanged commit; retain the existing snapshot until publication succeeds.
- Let each signed-in member create named read-only MCP Keys and revoke individual clients. Show new Keys once, store only hashes, and preserve existing Keys during metadata migration.
- Allow repository owners to rename a knowledge base and change private/shared visibility without reimporting or replacing its index. Apply visibility to Web and all MCP queries.
- Clear credential dialogs when closing them or returning to login; reject Key creation if the login session is revoked during the request.

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
