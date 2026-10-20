---
name: vmware-vnc
description: Control and inspect VMware or other VNC desktops from Windows through the bundled MCP server or CLI. Use for authorized screenshots, keyboard and mouse input, multi-VM sessions, and VNC connection diagnosis. Use vmrun for VMware power and guest operations.
license: Apache-2.0
---

# VMware VNC

通过项目内的 VNC MCP 服务操作指定远程桌面；无需接管宿主机鼠标。支持独立具名会话、原生尺寸截图、区域截图和键鼠操作。技能运行环境为 Windows、PowerShell 7、Python 3.11+。

本文件所在目录就是技能根目录。所有脚本、源码和参考资料都相对于该目录定位，不依赖原作者的工作区、全局 Python 包或当前工作目录。

## 授权与目标

- 对真实桌面使用 Computer Use 前，必须取得用户明确同意使用 Computer Use 的确切答复，并明确目标及操作范围。仅同意安装、配置、整理项目，或笼统同意某个方案，不构成该授权。已有授权在其目标和范围内持续有效。
- 连接时明确提供 host、port 和便于辨认的 session_id；不得猜测虚拟机目标。VMware 端口通常从 5901 起，以实际配置为准。
- VMware 电源和来宾命令优先用官方 vmrun；本技能负责需要画面的桌面交互。配置 VNC 或防火墙、重启虚拟机，应在用户授权的任务范围内进行。

## 准备工具

先确认当前聊天是否提供 `vmware_vnc` MCP 的 VNC 工具。技能安装与 MCP 注册是两个步骤，安装技能不会自动注册或重新加载工具。

- 首次安装、环境缺失或需要 MCP 注册时，读 [references/setup.md](references/setup.md)。依赖只用随包的 `requirements.lock.txt`，不自行升级。
- MCP 已可用时，直接使用当前聊天的工具；改过磁盘代码或配置不代表当前进程已经重载。
- MCP 暂不可用时，可以在已有桌面操作授权内用随包 CLI 完成独立操作。连续交互和多桌面任务优先配置 MCP，不反复启动 CLI 模拟会话。

## 操作顺序

1. `vnc_connect(host=..., port=..., session_id=...)`，保存返回的 `connection_id`。
2. 立即 `vnc_screenshot(session_id=..., connection_id=...)`，核对实际桌面、窗口和尺寸后再输入。
3. 所有具名会话的截图、输入、等待和断开，同时传 `session_id` 与本次连接的 `connection_id`。重连后换用新 ID。
4. 用新截图决定下一步。操作后再次截图验证结果，不把工具接受请求视为桌面目标已经完成。
5. 结束时断开本次使用的连接，释放保持的键鼠。不要关闭其他任务的会话。

需要具体工具参数、区域坐标、多桌面并行或 CLI 命令时，读 [references/operations.md](references/operations.md)。

## 操作边界

- 鼠标坐标始终使用桌面原生坐标；区域图内坐标须加上截图返回的原点。截图不缩放。
- 多桌面用不同 session_id。省略它会访问独立的 default 会话，不会自动选择刚连接的具名会话。`vnc_sessions` 用于缓存状态清单，`vnc_status` 用于实时状态。
- 失败、超时或取消的键鼠输入不得自动重放；已发送输入无法撤回。先观察当前画面和状态，再决定是否重连或继续。
- 默认空闲 300 秒回收；过期后需显式重连并重新截图。不要复用旧 connection_id。
- VMware 端的中文直接键盘输入尚未通过验证，剪贴板只支持 Latin-1。先确认来宾输入法状态，不承诺中文输入或粘贴成功。
- 密码通过本机环境变量提供，不写进技能、Git、示例或报告。截图和检查结果写入 `checks/results/`，私有连接资料放 `.local/`。

连接失败、画面不刷新、输入结果异常或遇到队列/会话错误时，读 [references/troubleshooting.md](references/troubleshooting.md)。

## 验证与维护

修改本技能的脚本或运行代码后，执行技能根目录的 `scripts/Test.ps1`。默认回归仅操作本机人工 RFB 桌面。真实端点验收属于 Computer Use，仍须先满足上述授权条件。

报告时分清模拟协议测试、真实端点测试和当前聊天工具加载状态。历史报告在 `docs/reports/`，不作为当前目标已连接或操作成功的证据。维护状态见 [docs/STATUS.md](docs/STATUS.md)，来源及本地修改见 [LOCAL-CHANGES.md](LOCAL-CHANGES.md)。
