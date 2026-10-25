# Validation

Run the checks from a clean source checkout:

```bash
uv sync --locked
.venv/bin/python -m unittest discover -s tests -v
node --check app/static/app.js
python3 scripts/prepare_release.py --check
```

Automated coverage includes authentication, closed-by-default registration, administrator-only member management, disabled-account session/Key revocation, legacy administrator migration, user isolation, shared read-only repositories, CSRF checks, read-only API Keys, encrypted credentials, Token rotation, origin matching, GitLab pagination/subgroups, GitHub organization/Enterprise API discovery, owner/clone-path validation, pagination budgets, idempotent imports, failed discovery rollback, snapshot publication consistency, interrupted workers, schema upgrades, source provenance, malformed MCP arguments, and Markdown restrictions.

Team access regressions also verify per-member named Key issuance, hash-only storage, individual revocation, the 20-Key limit, legacy Key migration without rotation, revoked-session races, and owner-only repository settings. Visibility changes are checked across Web search/source reads and all seven MCP tools without reindexing or creating sync jobs.

Exploration tests cover Chinese single-character recall without broadening multi-character queries, phrase ranking before candidate truncation, literal path/language filters, same-commit tokenizer upgrades, compact overviews, directory and file-outline pagination, exact-version checks, malformed manifests, and Python/Cargo dependency extraction. No test or benchmark establishes sub-second incremental synchronization; changed commits still use a full scan.

The private integration test creates a disposable Git HTTP server: group API calls require a fixture Token, Git fetch requires Basic authentication, and the final index is queried for known source evidence. It neither needs nor uses a real private repository. The fixture creates commit objects in a temporary bare repository; it does not commit the product checkout.

Public demos are optional and require network access:

```bash
python3 scripts/manage.py start
.venv/bin/python scripts/demo.py
```

The examples are fetched from their upstream repositories at runtime; their current commit and index counts may change. New source installations start with no imported repositories.

Container configuration can be checked with `docker compose config --quiet` and `docker compose build`. Run a test container on an unused loopback port with a fresh data volume; verify `/health`, initial login and MCP tool discovery. Do not reuse a private production data volume in tests.

These checks do not establish production hardening, arbitrary secret detection, complete call-graph accuracy, performance on every repository size, or multi-instance deployment support. See [security boundaries](../SECURITY.md).
