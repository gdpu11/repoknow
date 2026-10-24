# Third-party dependencies

RepoKnow's original source is licensed under [MIT](LICENSE). Dependencies keep their own licenses. The source archive contains dependency manifests, not vendored Python packages, imported repository contents, model weights, fonts, or external front-end assets.

The following direct runtime dependency licenses were checked against installed distribution metadata and bundled license files for the locked versions:

| Dependency | Locked version | Upstream license | Source |
|---|---|---|---|
| FastAPI | 0.161.1 | MIT | [fastapi/fastapi](https://github.com/fastapi/fastapi) |
| Uvicorn | 0.54.0 | BSD-3-Clause | [Kludex/uvicorn](https://github.com/Kludex/uvicorn) |
| cryptography | 46.0.7 | Apache-2.0 OR BSD-3-Clause | [pyca/cryptography](https://github.com/pyca/cryptography) |
| markdown-it-py | 4.2.0 | MIT, including upstream markdown-it notice | [executablebooks/markdown-it-py](https://github.com/executablebooks/markdown-it-py) |
| tree-sitter | 0.25.2 | MIT | [tree-sitter/py-tree-sitter](https://github.com/tree-sitter/py-tree-sitter) |
| tree-sitter-go | 0.25.0 | MIT | [tree-sitter/tree-sitter-go](https://github.com/tree-sitter/tree-sitter-go) |
| tree-sitter-java | 0.23.5 | MIT | [tree-sitter/tree-sitter-java](https://github.com/tree-sitter/tree-sitter-java) |
| tree-sitter-javascript | 0.25.0 | MIT | [tree-sitter/tree-sitter-javascript](https://github.com/tree-sitter/tree-sitter-javascript) |
| tree-sitter-python | 0.25.0 | MIT | [tree-sitter/tree-sitter-python](https://github.com/tree-sitter/tree-sitter-python) |
| tree-sitter-typescript | 0.23.2 | MIT | [tree-sitter/tree-sitter-typescript](https://github.com/tree-sitter/tree-sitter-typescript) |

`httpx` (BSD-3-Clause) is a development/test dependency. Transitive packages and platform-specific alternatives are recorded in `uv.lock`; their distribution license files remain with those packages when installed. For example, the lock includes certifi (MPL-2.0), typing-extensions (PSF-2.0), and OpenTelemetry API (Apache-2.0). Recheck this inventory when changing the lock or distributing built runtime images.

The optional public demo repositories are fetched separately at runtime. Their code, documents, and licenses remain the property and responsibility of their upstream projects; they are not redistributed in this source archive. Private repository authorization does not grant permission to redistribute its contents.
