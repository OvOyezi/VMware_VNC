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

2026-10-20 项目化整理将上述代码从 VMware_Personal 完整移植到 VMware_VNC。运行模块、main.py、包定义、固定依赖和许可证保持一致。重建环境，调整维护脚本及配置路径；性能和启动竞态的历史对照代码改为项目内固定测试样本。

VNC 调查报告位于 `docs/reports/`，测试结果、截图及补丁位于 `checks/results/`，手动操作场景位于 `.local/scenarios/`。按用户要求，旧代码和环境、重复归档、配置备份及临时验证目录已清除，仅以本项目维护。报告中的旧路径表示当时状态，当前工作方式以 README.md、AGENTS.md、docs/STATUS.md 为准。

运行依赖版本由 requirements.lock.txt 固定；维护检查仅使用现有依赖。尚未解决中文直接输入、中文剪贴板、持久增量帧与长期运行验证边界。

2026-10-21 将仓库根目录整理为可安装的 `vmware-vnc` skill：新增 SKILL.md、agents/openai.yaml、按需参考资料和 MCP 模板；本机硬编码的 codex-mcp.toml 改为 scripts/Get-McpConfig.ps1 按安装位置生成。README 区分本地 Git 安装、将来 GitHub 安装、技能发现和 MCP 注册。运行源码、Python 包版本与固定运行依赖未改动；未上传仓库，未自动安装技能或修改全局 Codex 配置。
