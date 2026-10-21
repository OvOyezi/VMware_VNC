---
name: vmware-vnc
description: Observe and interact with virtual-machine desktops through VNC when a task needs the visible interface. Use for screenshots, mouse and keyboard actions, Unicode text, visual condition waits and independent desktop sessions.
license: Apache-2.0
---

# VMware VNC

提供 VNC 画面观察与桌面交互能力，补充常规命令行工作方式无法完成或无法可靠核对的界面环节。来宾可以是 Windows、Linux 或其他提供 VNC 桌面的系统。

本文件所在目录就是技能根目录。MCP 和随包的 `vnc-mcp` CLI 提供同一套 VNC 能力；连续交互使用 MCP 可保留连接、会话和画面参考。

## 准备

- 首次安装、环境缺失或需要注册 MCP 时，读 [安装与接入](references/setup.md)。脚本从技能实际位置定位资源，依赖使用随包锁文件。
- 使用当前聊天已加载的 VNC 工具。修改磁盘代码或配置后，先确认 MCP 进程已重载；`vnc_diagnostics` 可核对运行版本。
- MCP 暂不可用时，可使用随包 CLI 执行独立的 VNC 操作。命令参数见 [操作参考](references/operations.md)。

## 调用顺序

1. 使用明确的 `host`、`port` 和 `session_id` 连接，或显式选择已配置的 `profile`；保存返回的 `connection_id`。
2. 立即截图，核对实际画面、窗口和原生尺寸后再输入。不要从示例推断目标端口或点击位置。
3. 具名会话的截图、输入、等待和断开同时传入 `session_id`、本次 `connection_id`。不同桌面使用不同会话名。
4. 根据新画面执行动作，再观察结果。区域截图内的坐标须加上截图原点，鼠标接口始终接收桌面原生坐标。
5. 结束时断开本次连接。重连后换用新 ID，并重新检查画面。

会话名可由调用方选取，避免覆盖其他任务的会话。省略 `session_id` 会访问独立的 default 会话，不会自动选择最近连接的具名会话。会话默认空闲 300 秒回收；旧连接 ID 失效后须显式重连。具体参数、多桌面与 CLI 示例见 [操作参考](references/operations.md)。

## 文本与画面条件

- 中文、多行文字和表情使用 `vnc_text` 或默认的 `vnc_type`。按 [文本输入](references/text-input.md)检查协商能力；可选来宾剪贴板后端的配置也在该文档。
- 任务限定纯 VNC 时，显式选择 `method="keys"`，或已协商的 VNC 剪贴板通道。逐键输入受来宾输入法影响；先核对目标应用中的输入模式，中文可通过已配置的来宾输入法输入拼音并选择候选词。
- 来宾辅助输入需要本次截图确认的 `focus_x/focus_y`。准备剪贴板后的点击会取消旧选区，替换内容前先明确清空。
- 等待变化或匹配前，用 `vnc_screenshot(remember=true)` 保存参考；stable 使用自己的起始画面。参考生命周期、区域和容差见 [画面条件](references/visual-waits.md)。
- `matched`、剪贴板校验和已发送粘贴分别描述工具完成的步骤。应用是否收到文本或完成任务，仍须检查实际结果。

## 异常与维护

失败、超时或取消的输入不自动重放；先检查当前画面与连接状态。保持的按键、鼠标按钮及会话资源由断开清理。连接、刷新、输入和队列问题见 [故障排查](references/troubleshooting.md)。

修改后运行 `scripts/Test.ps1`；默认检查只连接本机人工 RFB 服务。兼容范围及实机验收方法见 [兼容性](docs/COMPATIBILITY.md)，维护要求见 [CONTRIBUTING.md](CONTRIBUTING.md)。
