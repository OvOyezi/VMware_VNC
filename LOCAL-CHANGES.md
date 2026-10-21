# 来源与本地差异

以下保留各阶段变更与当时结论；当前版本能力见 0.3.0 条目。

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

运行依赖版本由 requirements.lock.txt 固定；维护检查仅使用现有依赖。2026-10-20 的整理阶段尚未解决中文直接输入、中文剪贴板、持久增量帧与长期运行验证问题，后续文字通道与耐久验证见 0.2.0 条目。

2026-10-21 将仓库根目录整理为可安装的 `vmware-vnc` skill：新增 SKILL.md、agents/openai.yaml、按需参考资料和 MCP 模板；本机硬编码的 codex-mcp.toml 改为 scripts/Get-McpConfig.ps1 按安装位置生成。README 区分本地 Git 安装、将来 GitHub 安装、技能发现和 MCP 注册。运行源码、Python 包版本与固定运行依赖未改动；未上传仓库，未自动安装技能或修改全局 Codex 配置。

## 2026-10-21：0.2.0 跨系统文字与稳定性

- 文字入口统一使用能力路由；新增 vnc_text、vnc_profiles、vnc_diagnostics。UTF-8 扩展优先，VMware 内置 VNC 使用显式配置的官方 vmrun 辅助通道。CLI 增加 text / profiles / doctor / --version，原 vnc_type 支持同一通道。
- 按 RFB 文档独立实现 text-only Extended Clipboard，包含惰性 Request 与 Z_SYNC_FLUSH 兼容、大小限制和异常断开；未复制 TigerVNC/noVNC 实现。
- 来宾辅助程序作为 Python 包资源随 wheel 安装；Windows 使用系统自带 PowerShell 5.1，Linux 使用 Python 3 与现有 X11/Wayland 剪贴板工具。文字是 JSON 数据，凭据仅由进程环境提供给官方 vmrun。Python 宿主最低版本统一为 3.11，以使用 tomllib。
- 显式端点绑定、剪贴板 hash 校验、临时文件清理、持有输入拒绝、可取消焦点等待；返回值区分已提交、数据已发送、已发粘贴与应用尚未验证。
- 选择无损 Hextile / RAW，并报告捕获耗时、收包字节与实际编码。实机发现固定依赖 ZRLE 行填充兼容问题，当前不协商该编码。
- 连接关闭时主动释放帧缓冲、协议包和剪贴板，降低循环引用带来的内存峰值。
- 增加人工服务与 mock 回归、独立文字窗口、只读实机耐久工具和本地准备的 GitHub Actions。固定运行依赖未变，源码仍只维护一份。

具体实机范围、性能代价与尚未验证项见 [兼容性记录](docs/COMPATIBILITY.md)。技能安装仍不自动注册 MCP；磁盘修改也不代表当前聊天的旧 MCP 进程已重载。

## 2026-10-21：0.3.0 画面条件与区域采样

- 增加 vnc_wait_for / CLI wait-for，支持变化、参考图匹配、固定窗口内稳定。等待不发送输入，条件未满足仍返回最后有效图，取消不自动重放之前的动作。
- 显式截图参考只驻留当前连接内存，每连接一张、16,777,216 像素上限；引用替换、重连与尺寸变化有明确边界。CLI 读取 PNG / JPEG 参考时校验文件大小、像素数与区域尺寸。
- 复用固定 vncdotool 的 framebufferUpdateRequest、画布和连接处理，以及现有 Pillow ImageChops。复现旧 expectScreen 的颜色直方图位置误判，改用逐像素比较；未升级依赖或复制上游未发布的视觉实现。
- 中间采样不反复编码；异步轮询释放队列，沿用连接 ID、操作预算和 MCP 取消。同一会话一次仅允许一个视觉等待，稳定窗口使用固定参考以捕获累积漂移。
- 区域请求使用现有 RFB 矩形 API，并处理非零原点的局部首帧。人工服务验证守规范服务端；VMware 内置 VNC fresh 实测仍返回全帧，流量收益不作跨服务端推广。
- 增加默认视觉回归和专用 Windows 来宾测试窗口；保留既有 104 项回归及历史报告，技能流程同步新增条件等待说明。
