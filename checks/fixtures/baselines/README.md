# 固定历史对照

这两个文件只供回归/性能复现使用，不属于生产后端。

- `typing-unbatched.py`：第二轮优化前的 client.py，用于 2000 字符完整接收的同机对照。
- `reactor-unlocked.py`：第三轮优化前的 client.py，用于延迟 reactor 启动的并发竞态复现。

原始文件来自旧工作区 checks/backups/vnc-deep-audit-20261020 与 vnc-multisession-20261020。迁移时按字节复制，后续测试从本目录解析，不依赖旧工作区。完整历史备份另存于本机 `.local/history/backups/`。
