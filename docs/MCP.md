# MCP clients

Run RepoKnow before connecting a client. The endpoint is `http://127.0.0.1:8090/mcp`, using Streamable HTTP with JSON responses. Authentication is `Authorization: Bearer <read_only_api_key>`; the Key is generated in that instance's private `.data/admin.json`.

The Key is separate from Git platform Tokens. Clients receive indexed evidence, not the Token used to fetch a private repository.

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
2. Use `knowledge_search` with a keyword. Omit `repository_id` for cross-repository search, or set it to limit the scope.
3. Call `knowledge_get_source` with the returned repository ID, exact commit, path and line range.
4. Use `knowledge_get_symbols` to find definitions and `knowledge_trace_dependency` for direct static call candidates.

Source reads are limited to 200 lines / 32 KiB per call. Only the current successful snapshot is retained; a commit mismatch returns an error rather than silently reading a newer version. Search can be truncated and returns a truncation flag. Call candidates are heuristic static matches, not a cross-service runtime trace.

All five MCP tools are read-only. Import and sync use the Web UI or authenticated management API, including the local `scripts/import_source.py` helper.
