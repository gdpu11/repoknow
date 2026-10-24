# Contributing

欢迎提交问题报告、文档修正和功能改进。请先描述具体使用场景，保持改动集中。

## 本地开发

需要 Linux/macOS、Python 3.10+、Git、uv；前端为原生 JavaScript/CSS，不需要 Node 包安装或构建。Node 仅用于语法检查。

```bash
uv sync --locked
.venv/bin/python -m unittest discover -s tests -v
node --check app/static/app.js
python3 scripts/prepare_release.py --check
```

测试会在临时目录创建 Git fixture 和本地 HTTP 服务，使用虚构的凭证；不需要真实 GitHub/GitLab Token，不操作开发者自己的仓库或数据库。受限沙箱可能需要允许回环端口绑定。

## 提交改动前

- 给行为变化添加有实际意义的回归测试；纯文档改动检查链接和命令即可。
- 凭证、用户数据库、克隆缓存、日志、客户端全局配置和本机截图不进入提交或问题报告。
- 使用 `example.com` / `example.invalid` 示例域名和明显的 fixture 值，避免复制真实业务项目、账号或 Token。
- 索引发布与权限校验必须保持事务一致性，MCP 保持只读。
- 修改依赖时同步 `uv.lock` 和 `requirements.txt`，并核对上游许可证。
- 添加公开文件时显式更新 `release-manifest.json`；导出器不会自动收集整个工作目录。

## 问题报告

请提供系统与 Python 版本、操作步骤、预期行为、实际结果和脱敏后的错误信息。可以使用公开最小复现仓库；不要上传私有源码或 `.data/`。安全问题请按 [SECURITY.md](SECURITY.md) 处理。

贡献按本项目 [MIT 许可](LICENSE) 提供；第三方代码或资源应保留其原许可与出处。
