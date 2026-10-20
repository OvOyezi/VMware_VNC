# 来源与本地差异

上游：[thanhvn-shinkoxd/VNC-MCP](https://github.com/thanhvn-shinkoxd/VNC-MCP/tree/abe747b4dc42bc936870959861a436c849aef482)，固定提交 `abe747b4dc42bc936870959861a436c849aef482`。只使用源码，未采用上游 dist 可执行程序。原作者及许可保留在 LICENSE、NOTICE、pyproject.toml。

本地运行改动：

- 修复 Unicode 事件调用后 vncdotool 同步代理丢失协议对象。
- 加入 VMware fresh 截图兼容模式，短截图连接与控制连接分离。
- MCP 默认 JPEG 80、4:4:4，保留 PNG、原生尺寸及区域原点。
- 真实断线、失败命令和目标切换后清理状态；按连接隔离剪贴板；关闭时释放保持的输入。
- 有界异步排队、协作取消、20 秒默认预算和连接代次校验，不自动重放输入。
- 编码移出 Twisted reactor；输入按小批次提交；等待不占用后端锁。
- CLI 要求显式地址和端口；仅 --ask-password 提示密码，修复 Windows 非交互挂起。
- 独立具名会话、每会话队列、默认 4 会话/16 请求/空闲 300 秒回收；新增 vnc_sessions。
- 修复首次并发连接重复启动全局 reactor；关闭槽位在清理完成前不得复用，失败清理隔离容量。

2026-10-20 项目化整理将上述代码从 VMware_Personal 复制到 VMware_VNC。运行模块、main.py、包定义、固定依赖和许可证保持一致。重建环境，调整维护脚本及配置路径；性能和启动竞态的历史对照代码改为项目内固定测试样本。

迁移前报告、补丁、完整源码备份和截图保存在 `.local/history/`，保持原始内容，旧路径仅表示当时状态。原始 README 另存为 `docs/README-before-project-migration.md`，其中旧路径和上游 EXE 示例属于历史说明，不是当前项目的安装入口。当前工作方式以 README.md、AGENTS.md、docs/STATUS.md 为准。

运行依赖版本由 requirements.lock.txt 固定；维护检查仅使用现有依赖。尚未解决中文直接输入、中文剪贴板、持久增量帧与长期运行验证边界。
