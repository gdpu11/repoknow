# MCP clients

Run RepoKnow before connecting a client. The endpoint is `http://127.0.0.1:8090/mcp`, using Streamable HTTP with JSON responses. Authentication is `Authorization: Bearer <read_only_api_key>`.

The Key is separate from Git platform Tokens. Clients receive indexed evidence, not the Token used to fetch a private repository.

## Personal Keys

Sign in with your own member account and open **MCP 接入** in the sidebar. Create a named Key for each client and copy it immediately; the complete value is shown only once, and only its hash is stored in the database. The page also displays the endpoint for the current instance. The client must be able to reach that address; a loopback address refers to the client's own machine.

Each account supports up to 20 Keys. They remain valid until revoked or the member is disabled. Revoke a specific Key in the same dialog to disconnect that client without affecting your login or other Keys. A Key cannot list, create or revoke Keys, import repositories, synchronize indexes or manage accounts. Lost Keys cannot be recovered; revoke them and create replacements.

Existing Keys remain valid when upgrading, and gain list/revocation metadata without rotation. The initial administrator Key is also listed; its original value remains in private `.data/admin.json`, but revoking it makes that value invalid even after a restart. Do not distribute the administrator Key to team members.

Keys use the account's current repository permissions on each request. A repository owner can change visibility in **知识库设置**: shared repositories are readable by all enabled members, and switching to private denies other members' subsequent queries through all seven tools. Previously retrieved evidence cannot be recalled from clients.

## Codex

Codex supports HTTP MCP configuration in its user configuration file. Follow the [official MCP configuration documentation](https://developers.openai.com/codex/mcp). The following is a placeholder example, not a working credential:

```toml
[mcp_servers.repoknow]
url = "http://127.0.0.1:8090/mcp"
bearer_token_env_var = "REPOKNOW_MCP_KEY"
```

Set `REPOKNOW_MCP_KEY` in the environment that launches your client. A desktop app may not inherit variables exported in an unrelated terminal. An alternative is the client's supported static Authorization header setting, stored only in its private local configuration. Do not copy authenticated configuration files into this repository.

After changing client configuration, reload MCP connections or start a new client session. This repository does not configure or restart clients automatically.

## Query workflow

1. Call `knowledge_catalog` to discover visible indexed repositories and their commits.
2. Call `knowledge_get_overview` with a repository ID for language statistics, top-level modules, entrypoint candidates and manifest dependencies. Modules and entrypoints are capped at 30 each, dependencies at 100; `overview.truncated` marks each cap. No file bodies are returned.
3. Browse a directory with `knowledge_list_files`: omit `path` for the root, or pass a repository-relative directory such as `app/`. Results contain direct child directories and indexed file metadata, not the original repository's complete tree. Each page has at most 200 entries (default 100), directories first, then files in path order.
4. Use `knowledge_search` with a keyword. Omit `repository_id` for cross-repository search, or set it to limit the scope. Optional `path_prefix` is a literal, case-sensitive path prefix; `language` matches the indexed language name without case sensitivity (for example `Go` or `Python`). Both filters apply before the candidate limit. Exact substrings in names and then in source text rank ahead of other matches; synonym expansion remains available for recall.
5. Use `knowledge_get_symbols` with `path` to obtain an exact file's outline, or with `q` to find definitions. `offset` and `limit` support pages of up to 300 definitions. Use `knowledge_trace_dependency` for direct static call candidates.
6. Call `knowledge_get_source` with a returned repository ID, exact commit, path and line range.

Directory and symbol pages return `commit_id`, `next_offset` and `truncated`. For subsequent pages, pass the previous `commit_id` as the optional `commit`; a changed index returns an error rather than mixing versions. The overview also accepts `commit` when a caller needs to pin its evidence version. Only source reads require `commit` on every call.

After upgrading, explicitly synchronize existing repositories once to rebuild the Chinese single-character index; this rebuild also runs when the commit is unchanged. Later unchanged-commit syncs reuse the new index. Queries containing multiple Han characters still use bigrams to avoid matching every common character separately.

Source reads are limited to 200 lines / 32 KiB per call. Only the current successful snapshot is retained; a commit mismatch returns an error rather than silently reading a newer version. Search can be truncated and returns a truncation flag. Call candidates are heuristic static matches, not a cross-service runtime trace.

All seven MCP tools are read-only. Import and sync use the Web UI or authenticated management API, including the local `scripts/import_source.py` helper.
