# VNC-MCP 测试记录

测试日期：2026-10-20（北京时间）。目标虚拟机：WIN11。

当前结论：Codex MCP 已统一改为 `vmware_vnc`，连接时明确指定 VNC 地址、端口和可选会话名，默认不绑定虚拟机。第三轮已支持真正的多会话；WIN11（5901）和 WIN10（5902）已通过注册配置在同一 MCP 进程中同时连接、并行截图和独立断开。WIN11 之前还通过点击、快捷键和英文输入验证。中文直接输入和中文剪贴板仍有下述限制。下面保留历轮测试的历史证据。

## 2026-10-20 第三轮：多会话与资源管理

65 项检查通过：基础 10 项、两种截图模式生命周期 18 项、上一轮深挖回归 16 项、新增多会话/资源管理 21 项。新增 `vnc_sessions`，总计 18 个工具。独立队列、取消和超时只影响各自会话；具名操作携带连接代次 ID，旧 ID 和切换前排队输入均被拒绝。默认最多 4 个会话、每会话 16 个未完成调用，空闲 300 秒回收，活动等待不会被误回收。

按 Codex 的真实注册配置新建进程：WIN11/WIN10 同时连接，各完成初始截图、三轮并行全图、区域截图；断开 WIN11 后 WIN10 保持原连接，重连 WIN11 后旧 ID 被拒绝。已查看两台实际截图。实机本轮没有键鼠输入；测试结束会话数为 0，配置哈希保持不变。独立列出的 5 项实机断言通过，不与上述 65 项模拟/后端检查重复计数。

记录：`checks/results/vnc-multi-smoke.json`、`vnc-multi-lifecycle.json`、`vnc-multi-deep-regression.json`、`vnc-multisession-checks.json`、`vmware-vnc-multisession-live.json`。完整可重放源码补丁为 `checks/results/vnc-mcp-multisession.patch`；改动前备份在 `checks/backups/vnc-multisession-20261020`。配置、依赖、VMX 和防火墙未改。当前聊天仍载入第二轮工具定义，需要重新加载 MCP 或重启 Codex 才能使用具名会话；尚未验证整个桌面应用重启。

## 2026-10-20 第二轮深挖与重新验收

本轮修复了真实复现的取消后继续输入、等待阻塞状态查询、编码阻塞全局 reactor，以及 CLI 隐式等待密码问题。加入有界调度、连接 ID 校验、区域截图、批量输入，并拆分调度/后端/图像处理。工具总数仍为 17，当前仍一次连接一台虚拟机。

44 项模拟协议、生命周期和并发检查通过；按 Codex 注册配置启动新 MCP 进程，实测 WIN11 → WIN10 → WIN11 的全图和区域截图成功，CLI 实机截图成功。真实虚拟机本轮未收到键鼠输入。详见 [深挖报告](VNC-DEEP-AUDIT.md)。旧进程仍需重新载入；尚未执行整个 Codex 桌面应用重启。

完整源码补丁：`checks/results/vnc-mcp-deep-optimized.patch`。本轮之前的源码备份：`checks/backups/vnc-deep-audit-20261020`。前一轮结果和补丁作为历史记录保留。

## 2026-10-20 第一轮优化与验收（历史）

已完成默认截图压缩、连接生命周期修复、切换失败清理、剪贴板隔离和正常退出时释放输入。默认 MCP 图片改为 JPEG，质量 80、4:4:4，不缩放分辨率；`vnc_screenshot(image_format="png")` 保留无损选项。CLI 默认截图仍为 PNG。目标没有环境默认值时，必须同时提供 host 和 port。

- 10 项基础 MCP/RFB 检查通过：`checks/results/vnc-mcp-optimized-smoke.json`。
- `refresh`、`fresh` 各 9 项生命周期回归通过：`checks/results/vnc-mcp-optimized-regression.json`。覆盖真实回环 TCP 的断线、截图超时、失败命令、切换失败、切换及正常退出释放输入。键鼠输入仅发往模拟桌面。
- 按 Codex 当前注册配置启动新进程，在同一 MCP 会话完成 WIN11 → WIN10 → WIN11 切换、连续 JPEG 和显式 PNG 截图：`checks/results/vmware-vnc-optimized-live.json`。这次实机验收只读，未发送键鼠输入。
- 原生分辨率分别为 WIN11 1680×1053、WIN10 1680×1050。两张默认 JPEG 已实际查看，界面及中文标签可辨认；有损压缩不保证逐像素一致。
- 同帧编码比较：WIN11 PNG 4,108,444 字节、JPEG 809,657 字节（减少 80.29%）；WIN10 PNG 628,196、JPEG 135,247（减少 78.47%）。JPEG 80、4:4:4 和优化选项按 [Pillow 文档](https://pillow.readthedocs.io/en/stable/handbook/image-file-formats.html#jpeg)核对。
- 全部工具仍为 17 个。断线后要求显式 `vnc_connect`；不自动重连或重放输入，避免在错误桌面执行操作。短截图连接失败时，不废弃健康的控制连接。
- Codex 配置、依赖版本、VMX 和防火墙没有在本次优化中改动。原来源、现有 Unicode 修复和 `fresh` 兼容模式保留。
- 当前聊天的旧 MCP 进程启动于本次代码修改之前，需要重新加载 MCP 或重启 Codex 才能使用新代码。按注册配置独立启动的新进程已验证，尚未执行整个 Codex 桌面应用重启。

完整可复现补丁：`checks/results/vnc-mcp-vmware-optimized.patch`，基于下文固定上游提交。优化前源码备份：`checks/backups/vnc-optimization-20261020`。旧的 `vnc-mcp-win11.patch` 保留作为此前版本的历史证据。

未验证：长期运行、密码认证与 Codex 桌面重开后的模型 WebSocket 行为。中文直接输入和 Latin-1 剪贴板限制仍在。截图体积改进不代表这些事项已解决。

## 通用配置验收

- 当前入口为 `vmware_vnc`，旧 `win11_vnc` 注册已移除；运行环境与源码目录未移动。
- 配置中移除了默认 `VNC_HOST`、`VNC_PORT`。未指定目标时连接会被拒绝；显式调用 `vnc_connect(host="127.0.0.1", port=5901)` 或 `port=5902` 可选择对应虚拟机。
- WIN11 的通用接入记录：`checks\results\vmware-vnc-generic-probe.json`；WIN10：`checks\results\vmware-vnc-win10-probe.json`。两次均通过实际 MCP stdio 枚举 17 个工具并取得截图，没有发送键鼠输入。
- 当前实现一次连接一台，重新调用 `vnc_connect` 可切换；新增虚拟机须先开启自身 VNC，并使用空闲且不重复的端口，从 5901 起分配。
- 通用摘要：`C:\Users\Administrator\Documents\VMware_Personal\VMware-vnc-access.json`。本次配置备份与核对结果见 `checks\results\vmware-vnc-migration.json`。

## 首次部署与真实验收（历史）

- 固定目录：`C:\Users\Administrator\Documents\VMware_Personal\tools\vnc-mcp`，独立 Python 位于 `.venv\Scripts\python.exe`；运行不再依赖临时目录。
- 目标：WIN11 的 VMware 内置 VNC，宿主机地址 `127.0.0.1:5901`。VNC 由用户在 VMware 设置中手动启用，访客机 IP 不再用作此服务端点。
- 原生画面：1680×1053；截图以原生 MCP ImageContent 返回。
- 已观察到点击最大化临时终端窗口、快捷键启动应用、`echo VNC-MCP-input-OK` 输出正确结果。
- 已验证连续 3 次截图成功；保持 Ctrl 键期间截图，再按 C 仍能取消终端输入，说明截图连接没有重置控制连接的按键状态。
- 已按 Codex 中登记的命令和环境变量重新启动 MCP、枚举 17 个工具、连接 WIN11 并取得截图。记录：`checks\results\WIN11-vnc-registered-probe.json`；画面：`checks\results\WIN11-vnc-registered-probe.png`。
- 最终源码的默认刷新模式又通过 9 组隔离协议回归检查：`checks\results\vnc-mcp-installed-smoke.json`。
- 本次打开的临时终端和记事本已正常关闭，YY 保留在原登录界面；最终画面在 `checks\results\WIN11-vnc-final-state.png`。没有使用宿主机全局键鼠控制。
- 部署摘要：`C:\Users\Administrator\Documents\VMware_Personal\WIN11-vnc-access.json`。与配置备份比较，已确认其他 Codex 配置的含义未改变。

## 第二项本地改动：VMware 截图兼容模式

真实端点出现了“新连接首帧成功，后续完整刷新请求无响应”的情况；只读诊断记录在 `checks\results\WIN11-vnc-refresh-diagnosis*.json`。显式协商像素格式、去掉扩展编码均未解决。尚未确定 VMware 内部的具体原因。

本地新增可选配置 `VNC_SCREENSHOT_MODE=fresh`：截图通过独立的共享连接取得完整首帧并关闭该短连接，键鼠继续使用原控制连接。源码未设置环境时仍默认 `refresh`；当前 Codex 通用注册使用 `fresh`，已验收 WIN11 和 WIN10。它是针对当前端点现象的兼容处理，不是 VMware 内部缺陷已修复的证明。

完整补丁（Unicode 修复与截图模式）：`C:\Users\Administrator\Documents\VMware_Personal\checks\results\vnc-mcp-win11.patch`。已通过 `git apply --check`，可干净应用到固定上游提交。

## 限制与配置变更

- WIN11 实测直接发送中文 Unicode 键盘字符未进入命令窗口；工具调用无报错不能作为文字已输入的依据。剪贴板仍限 Latin-1。
- 微软拼音会转换按字母发送的文本；输入命令前要切到英文输入状态。
- 未进行长时间运行、断网重连和密码认证验收。本次 VNC 端点没有设置密码。
- VMware 监听地址仍为所有网卡。本机已添加防火墙规则 `VMwarePersonal-WIN11-VNC-LocalOnly-5901`，阻止 vmware-vmx.exe 的 TCP 5901 网络入站；ActiveStore 中规则有效，本机回环连接通过。没有从另一台设备验证阻断效果；当前 Public、Private 防火墙启用，Domain 防火墙原本禁用且未改动。
- 首次登记名为 `win11_vnc`，现已改为通用的 `vmware_vnc`，其他 MCP 条目保留。首次配置备份：`C:\Users\Administrator\.codex\config.toml.pre-win11-vnc-20261020-155603.bak`。当前参考配置：`tools\vnc-mcp\codex-mcp.toml`。字段按 [OpenAI Docs](https://learn.chatgpt.com/docs/extend/mcp?surface=cli) 核对。
- 首次迁移时聊天仍载入旧 `win11_vnc` 名称；此名称后来已刷新为 `vmware_vnc`。本次代码优化仍需刷新已有 MCP 进程，见上方最新结果。
- WIN11 的 vmrun 管理调用仍需要加密密码；VNC 接入没有解除该要求。

撤销通用登记可运行 `codex mcp remove vmware_vnc`；撤销首次为 WIN11 添加的防火墙规则可运行 `Remove-NetFirewallRule -Name 'VMwarePersonal-WIN11-VNC-LocalOnly-5901'`。每台虚拟机的 VNC 开关位于自身 VMware 设置。无需用整份旧配置覆盖后续可能新增的其他设置。

## 初次隔离测试来源与环境（历史）

- 项目：https://github.com/thanhvn-shinkoxd/VNC-MCP
- 固定提交：`abe747b4dc42bc936870959861a436c849aef482`。
- 下载包 SHA-256：`8B7F383914A3A01E107300A735EC09BBFBEFEE4E6D457227E3D54D9244C39151`。
- 仅运行源码，未执行项目附带的 dist 程序。
- 隔离目录：`C:\Users\Administrator\AppData\Local\Temp\VNC-MCP-WIN11-check-20261019`。
- 原始源码：上述目录内 `source\VNC-MCP-abe747b4dc42bc936870959861a436c849aef482`；保持未修改。
- 修复版源码：上述目录内 `patched`。
- Python：上述目录内 `runtime\Scripts\python.exe`，版本 3.15.0。
- 主要依赖：`vncdotool==1.4.2`、`mcp==1.30.0`；完整冻结记录在 `checks\results\vnc-mcp-dependencies.txt`。`pip check` 通过。
- 项目声明版本为 0.1.0；MCP 握手中返回的 1.30.0 来自 SDK，不能作为项目自身版本。

## 原版错误及本地修复

经真实 MCP stdio 调用，`vnc_type("Hi-中\n")` 发出中文按下事件后失败，报错：`'NoneType' object has no attribute 'transport'`。

原因：项目通过 vncdotool 的同步代理直接调用底层 `keyEvent`。该方法返回 None，而代理会把返回值继续传递给后续调用，导致协议对象丢失。

本地修复：在协议类中新增 `unicode_key`，在 reactor 线程发送完整的按下/松开事件并返回协议对象，保持代理调用链。仅改动修复版 `src\vnc_mcp\client.py`；补丁保存在 `checks\results\vnc-mcp-unicode.patch`。

## 已验证范围

测试程序：`C:\Users\Administrator\Documents\VMware_Personal\checks\vnc_mcp_smoke.py`。

测试通过实际 MCP stdio 启动项目，并连接本机回环地址上的模拟 RFB 3.8 服务。画面为 64×48、使用无认证握手；没有控制宿主机或真实虚拟机桌面。

修复版通过以下 9 组检查：

1. MCP 初始化与工具枚举（共 17 个工具）。
2. RFB 连接及画面尺寸。
3. 截图返回原生 MCP ImageContent，PNG 像素与服务端画面一致。
4. 点击事件到达服务端，并能从后续截图观察画面变化。
5. 快捷键、ASCII 和 Unicode 键盘事件。
6. 滚轮及拖动调用。
7. Latin-1 剪贴板双向传输。
8. 拒绝超出画面范围的坐标。
9. 断开连接时释放保持按下的键和鼠标按钮。

原版失败记录：`checks\results\vnc-mcp-upstream.json`。

修复版通过记录：`checks\results\vnc-mcp-patched.json`。

限制：中文剪贴板被实现明确拒绝；Unicode 键盘事件只验证到模拟服务端，未验证 Windows 应用或输入法的实际中文输入。模拟服务未覆盖密码认证、VMware VNC 兼容性、长时间运行、断网重连等场景。

## WIN11 初次连接（VNC 启用前的历史）

使用修复版通过 MCP 调用 `vnc_connect`，目标为 `192.168.5.12:5900`。MCP 初始化成功，但 VNC 连接返回 `Timeout while waiting for client response`，未获得截图，未发送任何真实键鼠输入。

此前检查未发现 WIN11 的可达 VNC 入口：访客机 5900/5901 探测超时；宿主机 WIN11 的 vmware-vmx 进程没有 TCP 监听，VMX 明文部分未配置 `RemoteDisplay.vnc.*`。

连接记录：`checks\results\WIN11-vnc-probe.json`。

当前 WIN11 的 vmrun 管理调用要求虚拟机加密密码。此事实只说明当前命令行管理受限，不能推断该虚拟机一定不支持 VNC。

## 当时的接入计划（现已开通）

先为 WIN11 配置一个可达的 VNC 服务端，再进行真实截图、点击及文本输入验证。若使用 VMware 内置 VNC，应连接宿主机地址与配置端口；若使用 Windows 内的 VNC 服务，应连接访客机地址与服务端口。这次测试的 `192.168.5.12:5900` 是访客机地址，不能用它代替 VMware 内置 VNC 的宿主机端点。

初次隔离测试未修改 WIN11、未重启虚拟机、未修改 Codex 全局 MCP 配置。后续开通、固定部署和 Codex 登记结果见本页当前结果；现在使用固定目录中的环境。
