# 维护状态

更新日期：2026-10-21，Asia/Shanghai。

## 2026-10-21：0.3.0 画面条件与功能补强

本地分支 `codex/visual-conditions`，尚未上传仓库。新增 vnc_wait_for、截图 remember 引用与 CLI wait-for，等待区域变化、匹配或持续稳定；逐像素比较复用 Pillow，区域请求复用现有 vncdotool / RFB，39 个锁定依赖未升级。已核查 vncdotool 的已发布版本与固定 main 提交，以及 noVNC 的区域传输讨论，适配依据见 [画面条件](../references/visual-waits.md)。

已验证：136 项默认回归；WIN11 / WIN10 的 15 项实机视觉验收；双机 300.7 秒、241 轮持续条件测试（484 次截图、1,444 次条件调用）；相同实际截图的离线编码与比较耗时。条件失败、重连、取消与并发的范围详见 [兼容性记录](COMPATIBILITY.md)。

包含中文、空格和单引号的独立安装目录重新运行全部 136 项回归通过；wheel 在全新环境的基础 10、文字扩展 33、视觉 32 项检查通过。两个新 MCP 进程均列出 22 个工具，全局 Codex 配置未改变。独立技能行为检查确认了参考生命周期与超时处理，并据此补全 CLI 点击坐标说明。安装证据见本机 `checks/results/visual-install-validation.json`。

WIN11 专用测试窗口经来宾进程命令身份核对后关闭，其临时目录与清理报告已删除；宿主独立安装、wheel 构建和测试辅助目录已清除。既有 WIN11、WIN10 仍运行，主维护环境保留。Git 索引共 64 个文件，不含凭据、截图、实机结果或虚拟环境；没有配置远程。

当前聊天诊断仍为 0.2.0、21 个工具，新增的 22 工具进程已独立验证；需要宿主重载 MCP 后当前聊天才能调用 vnc_wait_for。视觉稳定或匹配不能替代应用完成校验；VMware 内置 VNC 的 fresh 端点仍发送全帧，区域请求的收益按实际服务端测量。

## 2026-10-21：0.2.0 维护方向与当前验收

维护方向为跨来宾系统的虚拟机可视化操作：VNC 负责画面与交互，官方 vmrun 负责 VMware 电源、文件和来宾命令。新增能力、边界和可重现验收集中在 [兼容性记录](COMPATIBILITY.md)与 [文本输入参考](../references/text-input.md)。以下旧记录保留当时结论。

本次在本地分支 `codex/hybrid-unicode-stability` 修改，未配置远程或上传 GitHub。核心版本为 0.2.0，固定 39 个运行依赖未升级，宿主最低 Python 统一为 3.11。

已验证：104 项默认人工服务/mock 回归通过；WIN11 30 轮及 64,000 字符、符号和换行输入精确读回；Linux 原生 UTF-8 与官方 vmrun 通道各 6 轮精确读回；Hextile 与 RAW 的 10 组像素一致性及流量/耗时对照；关闭帧缓冲释放的受控内存对照。结果详见兼容性记录。

三机 30 分钟耐久测试通过：901 轮、2,724 次截图、18 次计划重连与旧 ID 拒绝，结束后会话清零。新路径隔离安装的 104 项回归、fresh / refresh 耐久工具自检，以及 wheel 独立安装的 CLI、MCP 和 43 项基础/文字检查均通过。全局 Codex 配置未改变。

测试创建的 Linux VM 已关闭，临时镜像服务、防火墙规则与 Windows 测试窗口/目录已清理；既有 WIN11、WIN10 保持运行。临时 VM 文件和安装副本验收后删除。Git 不包含真实凭据、截图、实机结果或虚拟环境。

新工具已通过独立 stdio 进程验证，当前聊天原先加载的 18 工具进程尚未重新加载为 21 工具；磁盘代码更新不代表当前聊天已切换。Wayland、多日耐久与其他来宾仍需按实际环境另行验收。

## 2026-10-21 技能化整理

仓库根目录现在是 `vmware-vnc` 技能，包含 SKILL.md、agents/openai.yaml、references、assets、scripts 和唯一一份 src/vnc_mcp。新增脚本只生成按实际安装位置定位的 MCP 片段，不修改 Codex 配置。

本次在本地分支 `codex/package-vmware-vnc-skill` 整理，未配置远程、未上传 GitHub、未实际安装到用户技能目录。运行代码、包版本和固定依赖未调整。

本次已验证：

- skill-creator 的 quick_validate 通过，技能与安装文档的 17 个本地链接均可解析，默认允许自动发现。
- 原维护目录的 scripts/Test.ps1 全部通过：基础 10、生命周期 18、深挖 16、多会话 21，合计 65 项模拟检查。
- 从 Git 索引导出 45 个文件，确认不含 `.venv`、`.local`、检查结果、截图或凭据文件；以该导出替代网络下载，复用本机官方 skill-installer 的根目录参数解析、目录校验和复制流程完成隔离安装。没有访问 GitHub 下载端点。
- 隔离目录含中文、空格和单引号，重新运行 Setup 完成环境重建，39 个固定运行依赖逐一一致，pip check、外部工作目录导入与 CLI 通过；安装副本的完整 65 项模拟检查同样通过。
- 新脚本在外部工作目录生成的 TOML 可解析，command、args、cwd 均定位安装副本。按该片段启动 MCP，初始化并列出 18 个工具成功，无默认目标，缺少端点的连接请求被拒绝。
- 生成配置前没有环境时，脚本明确失败。全局 Codex 配置的 SHA-256 在隔离验证前后保持一致。真实虚拟机未连接，当前聊天的 MCP 未重新加载。

本机证据为 [安装验证摘要](../checks/results/skill-package-validation.json)、[隔离安装测试输出](../checks/results/skill-package-tests.stdout.log)及 `checks/results/skill-installed-*.json`；它们均不进入 Git。临时安装副本在验证后清除，不作为第二维护位置或备份保留。

本次没有真实虚拟机桌面操作授权，不执行真实端点连接、截图或输入。以下迁移记录保留其历史结论，不代表本次已重新完成实机验证。

## 2026-10-20 迁移记录

以下为当时的维护状态；其中分支、路径、端点和配置加载情况以当时证据为准。

主维护根目录：`C:\Users\Administrator\Documents\VMware_VNC`。本地 Git 分支为 main，未配置远程仓库。本项目是唯一的 VNC 维护位置；旧 VNC 副本、环境、备份和临时验证目录已清除。

## 本次迁移已验证

- main.py、7 个运行模块、包定义、2 个依赖文件、LICENSE、NOTICE 共 13 个文件与复制前 SHA-256 一致。VNC 行为没有在本次迁移中调整。
- Python 3.15.0 环境在本目录重建；39 个固定依赖版本逐一一致，pip check 通过。editable 包与 CLI 可用，从项目外部工作目录导入也指向新 src。
- 新 scripts/Test.ps1 全部通过：基础 10 项、两种模式生命周期 18 项、深挖回归 16 项、多会话 21 项，合计 65 项。两个历史对照样本已固定到项目内，测试不再依赖旧目录或临时源副本。
- Codex vmware_vnc 的 command、args、cwd 已指向本项目；其它配置字段经完整解析比较保持一致。本次迁移不保留配置备份。
- 此前迁移阶段按真实注册配置启动新 MCP 进程，WIN11/WIN10 同时连接、三轮并行全图、区域截图、独立断开和旧 ID 拒绝共 5 项实机断言通过；本次完整清理后的状态见后续条目。
- 日志、截图、手动操作场景和环境被 Git 排除。已确认没有旧路径的 Python 进程占用；整个 Codex 应用重启未在本次执行。
- 旧源码、备份和临时源删除后，65 项回归再次全部通过。按真实注册配置启动 MCP 并列出 18 个工具通过，空会话状态正常。
- 本次实机复测时，vmrun list 显示运行虚拟机为 0，5901/5902 无监听，因此连接被拒绝；未启动虚拟机或修改其配置。此前迁移时的实机验收已通过，但本次清理后未重新完成实机连接。

## 证据

- [基础检查](../checks/results/project-smoke.json)
- [生命周期](../checks/results/project-lifecycle.json)
- [深挖回归](../checks/results/project-deep.json)
- [多会话](../checks/results/project-multisession.json)
- [最近实机连接尝试（虚拟机关机）](../checks/results/project-live.json)
- [本机迁移记录](../.local/project-migration.json)
- [调查与验证报告](reports/README.md)

这些证据属于本机资料，未提交到 Git；新环境运行 scripts/Test.ps1 后生成自己的结果。

## 继续维护的边界

中文直接输入与 Latin-1 剪贴板限制未改变。fresh 仍采用独立共享连接取首帧，没有实现持久增量画面。多小时运行、真实网络故障和 Codex WebSocket 问题没有获得新增验证。
