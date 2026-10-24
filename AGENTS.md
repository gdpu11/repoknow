# Repository guidance

## Development

- Keep changes scoped and preserve the CPU-only, model-free architecture.
- Do not read, log, commit, or export `.data/`, credentials, database files, private Git caches, or local client configuration unless the user explicitly authorizes a task requiring that access.
- Run `uv sync --locked`, `.venv/bin/python -m unittest discover -s tests -v`, and `node --check app/static/app.js` for relevant changes.
- For public source preparation, run `python3 scripts/prepare_release.py --check`. Update `release-manifest.json` deliberately when adding public files or directories.
- Do not commit, push, publish, deploy, or change global client settings without explicit authorization.

## Authorized imports

- Use `.venv/bin/python scripts/import_source.py URL --wait` for an explicitly requested repository import.
- For a GitLab group or subgroup, add `--kind group`. Nested groups cannot be distinguished from repositories by URL shape alone.
- Reuse credentials configured in the Web UI; never put Tokens in chat, command arguments, Git URLs, or logs.
- A queued job is not a completed index. Check individual indexing results before reporting success.
- Group import is a one-time discovery, includes subgroups by default, and skips existing branches. Do not schedule work unless requested.
- MCP is read-only. Imports and sync use authenticated management APIs.
